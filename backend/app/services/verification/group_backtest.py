"""分组回测 — 5分组等权收益曲线 + Q1-Q5 spread + 单调性检验。

验证 score_and_rank() 输出的评分是否有区分度：高分股票是否真的跑赢低分。

用法：
  python -m app.services.verification.group_backtest --pool hot_leader --n_forward 5
  python -m app.services.verification.group_backtest --pool all --n_forward 5 --lookback 120 --sync
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("group_backtest")

_BACKEND = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_BACKEND))
_WEIGHTS_PATH = _BACKEND / "data" / "factor_weights.json"
_OUT_DIR = _BACKEND / "data" / "verification_results"

N_GROUPS = 5
MIN_CROSS_SECTION = 50
MIN_STOCK_DAYS = 40

# ── regime 借用 ic_analysis 的函数 ──
from .ic_analysis import _classify_regime, _load_index_data


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ── 横截面评分（本地实现，不调 score_and_rank 避免依赖 rows 列约定） ──

def _rankdata(vals: list[float]) -> list[float]:
    n = len(vals)
    if n == 0:
        return []
    indexed = sorted((v, i) for i, v in enumerate(vals))
    out = [0.0] * n
    j = 0
    while j < n:
        k = j
        while k < n and indexed[k][0] == indexed[j][0]:
            k += 1
        rank_avg = (j + k - 1) / 2.0 / (n - 1) if n > 1 else 0.5
        for m in range(j, k):
            out[indexed[m][1]] = rank_avg
        j = k
    return out


def _winsorize_mad(series: list[float], n: float = 5.0) -> list[float]:
    m = sorted(series)[len(series) // 2]
    abs_dev = sorted(abs(v - m) for v in series)
    mad = abs_dev[len(abs_dev) // 2] * 1.4826
    if mad == 0:
        mad = sorted(abs(v - m) for v in series)[-1] or 1.0
    lo, hi = m - n * mad, m + n * mad
    return [max(lo, min(hi, v)) for v in series]


def _zscore(series: list[float]) -> list[float]:
    n = len(series)
    if n < 2:
        return [0.0] * n
    mean = sum(series) / n
    std = (sum((v - mean) ** 2 for v in series) / (n - 1)) ** 0.5
    if std == 0:
        return [0.0] * n
    return [(v - mean) / std for v in series]


def _clip(series: list[float], lo: float, hi: float) -> list[float]:
    return [max(lo, min(hi, v)) for v in series]


def _minmax(series: list[float]) -> list[float]:
    mn, mx = min(series), max(series)
    if mx == mn:
        return [0.5] * len(series)
    return [(v - mn) / (mx - mn) for v in series]


def _cross_sectional_score(factor_dict: dict[str, list[float]], config: dict) -> list[float]:
    """给定横截面因子值，返回每只股票的评分（0-1）。"""
    fields = config["fields"]
    weights = config["weights"]
    zscore_fields = set(config.get("zscore_fields", []))
    invert_fields = set(config.get("invert_fields", []))

    n = len(next(iter(factor_dict.values()), []))
    scores = [0.0] * n

    for fi, f in enumerate(fields):
        vals = factor_dict.get(f)
        if not vals:
            continue
        w = weights[fi] if fi < len(weights) else 0

        if f == "vol_ratio":
            dist = [abs(v - 1.0) for v in vals]
            normed = [1.0 - r for r in _rankdata(dist)]
        elif f in zscore_fields:
            ws = _winsorize_mad([v if v is not None else 0 for v in vals], 5.0)
            z = _zscore(ws)
            c = _clip(z, -3.0, 3.0)
            normed = _minmax(c)
            if f in invert_fields:
                normed = [1.0 - v for v in normed]
        elif f in invert_fields:
            normed = [1.0 - r for r in _rankdata(vals)]
        else:
            normed = _rankdata(vals)

        for i in range(n):
            scores[i] += w * normed[i]

    return scores


# ── 主分析 ──

async def analyze_group(pool_name: str, n_forward: int, lookback: int, sync: bool = False) -> dict | None:
    os.makedirs(_OUT_DIR, exist_ok=True)

    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        fw = json.load(f)

    config = fw.get(pool_name)
    if not config:
        logger.error(f"Pool '{pool_name}' not found. Available: {list(fw)}")
        return None
    fields = config["fields"]

    logger.info(f"Pool: {pool_name} | Forward: T+{n_forward} | Groups: {N_GROUPS} | Lookback: {lookback}")

    if sync:
        from .ic_analysis import _sync_daily_basic, _sync_stock_daily
        await _sync_daily_basic()
        await _sync_stock_daily(lookback + n_forward + 30)

    from app.core.database import async_session as get_session

    async with get_session() as session:
        # 获取交易日
        r = await session.execute(text(
            "SELECT trade_date FROM stock_daily "
            "GROUP BY trade_date HAVING COUNT(*) >= :m "
            "ORDER BY trade_date DESC LIMIT :n"
        ), {"m": MIN_CROSS_SECTION, "n": lookback + n_forward + 30})
        dates = [row[0] for row in r.fetchall()][::-1]

        if len(dates) < 30:
            logger.error(f"Not enough dates: {len(dates)}")
            return None

        logger.info(f"Dates: {len(dates)}")

        # 加载全市场数据
        d0, d1 = dates[0], dates[-1]
        r = await session.execute(text("""
            SELECT d.ts_code, d.trade_date, d.close, d.volume, d.pct_chg,
                   db.pe, db.pb, db.total_mv, db.turnover_rate
            FROM stock_daily d
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.ts_code IN (
                SELECT ts_code FROM stock_daily
                WHERE trade_date BETWEEN :d0 AND :d1
                  AND ts_code NOT LIKE '%ST%' AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '920%'
                GROUP BY ts_code HAVING COUNT(*) >= :min_days
            )
              AND d.trade_date BETWEEN :d0 AND :d1
            ORDER BY d.ts_code, d.trade_date
        """), {"d0": d0, "d1": d1, "min_days": MIN_STOCK_DAYS})

        rows = r.mappings().all()
        stock_data: dict[str, dict[str, dict]] = {}  # {code: {date: row}}
        for row in rows:
            c = row["ts_code"]
            if c not in stock_data:
                stock_data[c] = {}
            stock_data[c][row["trade_date"]] = {
                "close": float(row["close"] or 0),
                "volume": float(row["volume"] or 0),
                "pct_chg": float(row["pct_chg"] or 0),
                "pe": float(row["pe"]) if row["pe"] and float(row["pe"]) > 0 else None,
                "pb": float(row["pb"]) if row["pb"] and float(row["pb"]) > 0 else None,
                "total_mv": float(row["total_mv"]) if row["total_mv"] and float(row["total_mv"]) > 0 else None,
                "turnover": float(row["turnover_rate"]) if row["turnover_rate"] and float(row["turnover_rate"]) > 0 else None,
            }

    stocks = list(stock_data.keys())
    logger.info(f"Universe: {len(stocks)} stocks")

    # ── 加载大盘指数 + 预分类每天的市场状态 ──
    async with get_session() as session:
        idx_data = await _load_index_data(session, dates)
    idx_closes = [idx_data.get(d) for d in dates]
    last_valid = None
    for i in range(len(idx_closes)):
        if idx_closes[i] is None:
            idx_closes[i] = last_valid
        else:
            last_valid = idx_closes[i]
    regime_cache: dict[str, str] = {}
    for di, td in enumerate(dates):
        regime_cache[td] = _classify_regime(idx_closes, di) if idx_closes[di] else "unknown"

    # ── 逐日分组（按 regime 分段） ──
    group_returns: dict[int, list[float]] = {g: [] for g in range(N_GROUPS)}
    # regime_returns: {regime: {group: [ret, ...]}}
    regime_returns: dict[str, dict[int, list[float]]] = {}
    date_labels: list[str] = []
    skipped = 0

    for di, td in enumerate(dates):
        if di + n_forward >= len(dates):
            break

        td_fwd = dates[di + n_forward]

        # 收集当日横截面因子值
        factor_dict: dict[str, list[float]] = {f: [] for f in fields}
        row_tuples: list[tuple[str, dict]] = []  # [(code, row), ...]

        for code in stocks:
            row = stock_data[code].get(td)
            if not row or row["close"] <= 0:
                continue
            row_fwd = stock_data[code].get(td_fwd)
            if not row_fwd or row_fwd["close"] <= 0:
                continue

            # 计算因子值
            fv_ok = True
            for f in fields:
                fv = _factor_at_date(stock_data[code], dates, di, f)
                if fv is None:
                    fv_ok = False
                    break
                factor_dict[f].append(fv)
            if not fv_ok:
                for f in fields:
                    if len(factor_dict[f]) > len(row_tuples):
                        factor_dict[f].pop()
                continue

            row_tuples.append((code, row, row_fwd))

        if len(row_tuples) < MIN_CROSS_SECTION:
            skipped += 1
            continue

        # 评分
        scores = _cross_sectional_score(factor_dict, config)

        # 分组
        sorted_idx = sorted(range(len(scores)), key=lambda i: scores[i])
        group_size = len(sorted_idx) // N_GROUPS
        groups = [[] for _ in range(N_GROUPS)]
        for j, idx in enumerate(sorted_idx):
            g = min(j // group_size, N_GROUPS - 1)
            groups[g].append(idx)

        # 计算每组 T+N 等权收益
        for g in range(N_GROUPS):
            if not groups[g]:
                continue
            rets = []
            for idx in groups[g]:
                code, row, row_fwd = row_tuples[idx]
                ret = (row_fwd["close"] - row["close"]) / row["close"]
                rets.append(ret)
            if rets:
                group_returns[g].append(sum(rets) / len(rets))

        date_labels.append(td)
        regime = regime_cache.get(td, "unknown")
        regime_returns.setdefault(regime, {g: [] for g in range(N_GROUPS)})
        for g in range(N_GROUPS):
            if groups[g]:
                rets = []
                for idx in groups[g]:
                    code, row, row_fwd = row_tuples[idx]
                    rets.append((row_fwd["close"] - row["close"]) / row["close"])
                if rets:
                    regime_returns[regime][g].append(sum(rets) / len(rets))

    if skipped:
        logger.info(f"Skipped {skipped}/{len(dates)} dates")

    # ── 累计收益 ──
    cum_returns: dict[int, list[float]] = {}
    for g in range(N_GROUPS):
        cum = []
        running = 0.0
        for r in group_returns[g]:
            running = (1 + running) * (1 + r) - 1
            cum.append(round(running * 100, 2))
        cum_returns[g] = cum

    # Spread: Q0 - Q4
    spread = []
    for i in range(min(len(group_returns[0]), len(group_returns[N_GROUPS - 1]))):
        spread.append(round(group_returns[0][i] - group_returns[N_GROUPS - 1][i], 6))

    # 单调性
    mean_returns = {g: (sum(group_returns[g]) / len(group_returns[g]) if group_returns[g] else 0) for g in range(N_GROUPS)}
    monotonic_ok = all(mean_returns[i] >= mean_returns[i + 1] for i in range(N_GROUPS - 1))

    # ── 写 CSV ──
    ts = _now()
    path = _OUT_DIR / f"group_backtest_{pool_name}_F{n_forward}_{ts}.csv"
    max_len = max(len(v) for v in group_returns.values())
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        header = ["date"]
        for g in range(N_GROUPS):
            header.append(f"Q{g}_ret")
            header.append(f"Q{g}_cum")
        header.append("spread")
        w.writerow(header)
        for i in range(max_len):
            row = [date_labels[i] if i < len(date_labels) else ""]
            for g in range(N_GROUPS):
                row.append(round(group_returns[g][i] * 100, 4) if i < len(group_returns[g]) else "")
                row.append(cum_returns[g][i] if i < len(cum_returns[g]) else "")
            row.append(round(spread[i] * 100, 4) if i < len(spread) else "")
            w.writerow(row)

    logger.info(f"Group backtest → {path}")

    # ── 日志输出 ──
    print(f"\n{'='*70}")
    print(f"  Group Backtest — {pool_name}  |  Forward T+{n_forward}  |  {N_GROUPS} groups")
    print(f"{'='*70}")

    all_regimes = sorted(set(regime_returns.keys()))
    for regime in all_regimes:
        rets_by_g = regime_returns[regime]
        rname = {"bull": "单边上涨", "bear": "单边下跌", "range": "震荡", "volatile": "高波动", "unknown": "未知"}.get(regime, regime)
        n_days = max(len(rets_by_g.get(g, [])) for g in range(N_GROUPS))
        print(f"\n  [{rname}] ({n_days} days)")
        print(f"  {'Group':<10}{'Mean Daily Ret':>16}{'Ann. Ret':>12}{'Sharpe':>10}")
        print("  " + "-" * 64)
        for g in range(N_GROUPS):
            rets = rets_by_g.get(g, [])
            if rets:
                m = sum(rets) / len(rets)
                std = (sum((v - m) ** 2 for v in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0
                ann = (1 + m) ** (252 / n_forward) - 1 if m > -1 else 0
                sr = m / std * (252 / n_forward) ** 0.5 if std > 0 else 0
            else:
                m = ann = sr = 0
            print(f"    Q{g:<9}{m*100:>+13.4f}%{ann*100:>+11.2f}%{sr:>+10.3f}")

        # monotonic check per-regime
        means = {g: (sum(rets_by_g[g]) / len(rets_by_g[g]) if rets_by_g.get(g) else 0) for g in range(N_GROUPS)}
        mono = all(means[i] >= means[i + 1] for i in range(N_GROUPS - 1))
        spread_regime = []
        r0 = rets_by_g.get(0, [])
        r4 = rets_by_g.get(N_GROUPS - 1, [])
        for i in range(min(len(r0), len(r4))):
            spread_regime.append(r0[i] - r4[i])
        avg_spread = (sum(spread_regime) / len(spread_regime) * 100) if spread_regime else 0
        print(f"    Q0-Q{N_GROUPS-1} spread: {avg_spread:.3f}% avg | Monotonic: {'PASS' if mono else 'FAIL'}")

    # 全窗口
    print(f"\n  [全窗口]")
    print(f"  {'Group':<10}{'Mean Daily Ret':>16}{'Ann. Ret':>12}{'Sharpe':>10}")
    print("  " + "-" * 64)
    for g in range(N_GROUPS):
        rets = group_returns[g]
        if rets:
            m = sum(rets) / len(rets)
            std = (sum((v - m) ** 2 for v in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0
            ann = (1 + m) ** (252 / n_forward) - 1 if m > -1 else 0
            sr = m / std * (252 / n_forward) ** 0.5 if std > 0 else 0
        else:
            m = ann = sr = 0
        print(f"  Q{g:<9}{m*100:>+13.4f}%{ann*100:>+11.2f}%{sr:>+10.3f}")
    print("-" * 70)
    print(f"  Q0-Q{N_GROUPS-1} spread: {sum(spread)/len(spread)*100:.3f}% avg"
          if spread else "  No spread data")
    print(f"  Monotonic: {'PASS' if monotonic_ok else 'FAIL'}")
    print(f"  CSV: {path}")

    return {
        "pool": pool_name,
        "n_forward": n_forward,
        "mean_daily_ret": mean_returns,
        "cum_returns": cum_returns,
        "spread": spread,
        "monotonic": monotonic_ok,
        "csv_path": str(path),
    }


# ── 因子时序计算 ──

def _factor_at_date(stock_series: dict[str, dict], dates: list[str], di: int, field: str) -> float | None:
    td = dates[di]
    row = stock_series.get(td)
    if not row:
        return None

    if field == "pct_chg":
        return row["pct_chg"]
    if field in ("pe", "pb", "total_mv", "turnover"):
        v = row.get(field)
        return float(v) if v is not None and v > 0 else None

    # 历史窗口因子
    closes = []
    volumes = []
    for j in range(max(0, di - 30), di + 1):
        r = stock_series.get(dates[j])
        if r:
            closes.append(r["close"])
            volumes.append(r["volume"])
        else:
            closes.append(closes[-1] if closes else 0)
            volumes.append(volumes[-1] if volumes else 0)

    i = len(closes) - 1

    if field == "chg3":
        if i >= 3 and closes[i - 3] > 0:
            return (closes[i] - closes[i - 3]) / closes[i - 3] * 100
    elif field == "chg5":
        if i >= 5 and closes[i - 5] > 0:
            return (closes[i] - closes[i - 5]) / closes[i - 5] * 100
    elif field == "chg10":
        if i >= 10 and closes[i - 10] > 0:
            return (closes[i] - closes[i - 10]) / closes[i - 10] * 100
    elif field == "vol_ratio":
        if i >= 19:
            avg_vol = sum(volumes[-20:]) / 20
            if avg_vol > 0:
                return volumes[i] / avg_vol
    elif field == "dist_from_low":
        if i >= 19:
            lows = closes[-20:]
            mn = min(lows)
            if mn > 0:
                return (closes[i] - mn) / mn * 100
    elif field == "ma_slope":
        if i >= 11:
            ma10 = sum(closes[-10:]) / 10
            ma10_2 = sum(closes[-12:-2]) / 10
            if ma10_2 > 0:
                return (ma10 - ma10_2) / ma10_2 * 100
    elif field == "drawdown":
        if i >= 19:
            highs = closes[-20:]
            peak = max(highs)
            if peak > 0:
                return (peak - closes[i]) / peak * 100
    elif field == "chg3_recent":
        if i >= 3 and closes[i - 3] > 0:
            return (closes[i] - closes[i - 3]) / closes[i - 3] * 100

    return None


# ── CLI ──

async def main():
    parser = argparse.ArgumentParser(description="分组回测")
    parser.add_argument("--pool", default="all", help="池名或 'all'")
    parser.add_argument("--n_forward", type=int, default=5, help="前向N日（默认5）")
    parser.add_argument("--lookback", type=int, default=120, help="回溯交易日数（默认120）")
    parser.add_argument("--sync", action="store_true", help="分析前先补数据")
    args = parser.parse_args()

    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        fw = json.load(f)

    pools = list(fw.keys()) if args.pool == "all" else [args.pool]
    unknown = [p for p in pools if p not in fw]
    if unknown:
        logger.error(f"Unknown pools: {unknown}")
        return

    t0 = time.perf_counter()
    for p in pools:
        await analyze_group(p, args.n_forward, args.lookback, sync=args.sync)
    logger.info(f"Total: {time.perf_counter() - t0:.1f}s for {len(pools)} pool(s)")


if __name__ == "__main__":
    asyncio.run(main())
