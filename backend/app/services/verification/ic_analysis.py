"""因子 IC 分析 — 横截面 Rank IC + IC_IR + 衰减曲线。

纯离线：只读本地 SQLite，零 API 调用。
用法：
  python -m app.services.verification.ic_analysis --pool hot_leader
  python -m app.services.verification.ic_analysis --pool all --lookback 120 --sync
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
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ic_analysis")

# ── 路径 ──
_BACKEND = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_BACKEND))
_WEIGHTS_PATH = _BACKEND / "data" / "factor_weights.json"
_OUT_DIR = _BACKEND / "data" / "verification_results"

FORWARD_N = [1, 3, 5, 10, 20]
MIN_STOCK_DAYS = 60
MIN_CROSS_SECTION = 50

# ── 市场状态分类 ──
REGIME_LOOKBACK = 20  # MA20 窗口
BULL_SLOPE = 0.3      # MA20 斜率 > +0.3%/天 → 单边上涨
BEAR_SLOPE = -0.3     # MA20 斜率 < -0.3%/天 → 单边下跌


def _classify_regime(idx_closes: list[float], di: int) -> str:
    """基于大盘指数截至 di 天的 MA20 斜率 + 位置 + 波动率分类。

    返回: "bull" | "bear" | "range" | "volatile"
    """
    if di < REGIME_LOOKBACK + 1:
        return "unknown"

    window = idx_closes[di - REGIME_LOOKBACK:di + 1]
    # 窗口中有 None 无法分类，退回 unknown
    if any(v is None for v in window):
        return "unknown"
    ma20 = sum(window) / REGIME_LOOKBACK
    current = idx_closes[di]

    # MA20 斜率: 两端均值的差 / 时间跨度
    first_half = window[:10]
    last_half = window[-10:]
    slope = ((sum(last_half) / 10) - (sum(first_half) / 10)) / (sum(first_half) / 10) * 100 / 10

    # 波动率: 20日振幅 / 均值的标准差
    mean_w = sum(window) / len(window)
    var = sum((v - mean_w) ** 2 for v in window) / len(window)
    vol = (var ** 0.5) / mean_w * 100 if mean_w > 0 else 0

    # 高波动优先
    if vol > 3.0:
        return "volatile"

    if slope > BULL_SLOPE and current > ma20:
        return "bull"
    elif slope < BEAR_SLOPE and current < ma20:
        return "bear"
    else:
        return "range"


async def _load_index_data(session, dates: list[str]) -> dict[str, float]:
    """加载上证指数 000001.SH 的收盘价序列，返回 {date: close}。"""
    if not dates:
        return {}
    r = await session.execute(text(
        "SELECT trade_date, close FROM stock_daily "
        "WHERE ts_code = '000001.SH' AND trade_date BETWEEN :d0 AND :d1 "
        "ORDER BY trade_date"
    ), {"d0": dates[0], "d1": dates[-1]})
    return {row[0]: float(row[1] or 0) for row in r.fetchall()}


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _rankdata(vals: list[float]) -> list[float]:
    """返回 [0,1] 百分位排名（处理并列值取平均）。"""
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


def _spearman(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation（等价于 Pearson on ranks）。"""
    n = len(x)
    if n < 3:
        return 0.0
    rx = _rankdata(x)
    ry = _rankdata(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    sx = sy = sxy = 0.0
    for i in range(n):
        dx = rx[i] - mx
        dy = ry[i] - my
        sx += dx * dx
        sy += dy * dy
        sxy += dx * dy
    denom = (sx * sy) ** 0.5
    return sxy / denom if denom != 0 else 0.0


async def _get_trade_dates(session, lookback_days: int) -> list[str]:
    """取最近 N 个交易日列表（ASC）。"""
    r = await session.execute(text(
        "SELECT trade_date FROM stock_daily "
        "GROUP BY trade_date HAVING COUNT(*) >= :m "
        "ORDER BY trade_date DESC LIMIT :n"
    ), {"m": MIN_CROSS_SECTION, "n": lookback_days + max(FORWARD_N) + 30})
    return [row[0] for row in r.fetchall()][::-1]


async def _load_universe(session, dates: list[str]) -> dict[str, list[dict]]:
    """加载全市场股票的日线 + 基本面数据，按 ts_code 分组。

    返回 {ts_code: [{date, close, volume, pct_chg, pe, pb, total_mv, turnover}, ...], ...}
    """
    if not dates:
        return {}
    d0, d1 = dates[0], dates[-1]

    # 筛选股票池：最近60天有>=MIN_STOCK_DAYS条记录，排除ST/688/920
    r = await session.execute(text("""
        SELECT ts_code FROM stock_daily
        WHERE trade_date BETWEEN :d0 AND :d1
          AND ts_code NOT LIKE '%ST%'
          AND ts_code NOT LIKE '688%'
          AND ts_code NOT LIKE '920%'
        GROUP BY ts_code
        HAVING COUNT(*) >= :min_days
    """), {"d0": d0, "d1": d1, "min_days": MIN_STOCK_DAYS})
    codes = [row[0] for row in r.fetchall()]
    logger.info(f"Universe: {len(codes)} stocks with >= {MIN_STOCK_DAYS} trading days")

    # 批量加载日线
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
    stock_data: dict[str, list[dict]] = {}
    for row in rows:
        c = row["ts_code"]
        if c not in stock_data:
            stock_data[c] = []
        stock_data[c].append({
            "date": row["trade_date"],
            "close": float(row["close"] or 0),
            "volume": float(row["volume"] or 0),
            "pct_chg": float(row["pct_chg"] or 0),
            "pe": float(row["pe"]) if row["pe"] and float(row["pe"]) > 0 else None,
            "pb": float(row["pb"]) if row["pb"] and float(row["pb"]) > 0 else None,
            "total_mv": float(row["total_mv"]) if row["total_mv"] and float(row["total_mv"]) > 0 else None,
            "turnover": float(row["turnover_rate"]) if row["turnover_rate"] and float(row["turnover_rate"]) > 0 else None,
        })
    return stock_data


# ── 前向收益（标签） ──

def _forward_returns(closes: list[float]) -> dict[int, list[float | None]]:
    """一次计算所有 forward N 的标签。"""
    n_days = len(closes)
    fwd: dict[int, list[float | None]] = {k: [None] * n_days for k in FORWARD_N}
    for N in FORWARD_N:
        for i in range(n_days - N):
            if closes[i] != 0:
                fwd[N][i] = (closes[i + N] - closes[i]) / closes[i]
    return fwd


# ── 主分析 ──

async def analyze_pool(pool_name: str, lookback: int, sync: bool = False) -> dict | None:
    """对单个池跑 IC 分析。"""
    os.makedirs(_OUT_DIR, exist_ok=True)

    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        fw = json.load(f)

    config = fw.get(pool_name)
    if not config:
        logger.error(f"Pool '{pool_name}' not found in factor_weights.json. "
                       f"Available: {list(fw.keys())}")
        return None

    fields = config["fields"]
    logger.info(f"Pool: {pool_name} | Factors: {fields} | Lookback: {lookback} days")

    # ── 数据同步（可选） ──
    from app.core.database import async_session as get_session

    if sync:
        # 先查需要覆盖的交易日，补 daily_basic
        async with get_session() as session:
            prelim_dates = await _get_trade_dates(session, lookback + max(FORWARD_N) + 10)
        await _sync_daily_basic(prelim_dates)
        await _sync_stock_daily(lookback + max(FORWARD_N) + 30)

    async with get_session() as session:
        dates = await _get_trade_dates(session, lookback + max(FORWARD_N) + 10)
        if len(dates) < 30:
            logger.error(f"Not enough trading dates: {len(dates)}")
            return None

        logger.info(f"Trading dates: {len(dates)} ({dates[0]} ~ {dates[-1]})")

        stock_data = await _load_universe(session, dates)
        if len(stock_data) < MIN_CROSS_SECTION:
            logger.error(f"Not enough stocks: {len(stock_data)}")
            return None

        # ── 加载大盘指数 + 预分类每天的市场状态 ──
        idx_data = await _load_index_data(session, dates)
        idx_closes = [idx_data.get(d) for d in dates]
        # 对缺失值做前向填充
        last_valid = None
        for i in range(len(idx_closes)):
            if idx_closes[i] is None:
                idx_closes[i] = last_valid
            else:
                last_valid = idx_closes[i]
        regime_cache: dict[str, str] = {}
        for di, td in enumerate(dates):
            regime_cache[td] = _classify_regime(idx_closes, di) if idx_closes[di] else "unknown"

        regime_counts: dict[str, int] = {}
        for r in regime_cache.values():
            regime_counts[r] = regime_counts.get(r, 0) + 1
        logger.info(f"Regime distribution: {regime_counts}")

    # ── 逐日 IC 计算（按状态分段） ──
    ic_rows: list[dict] = []  # [{date, factor, N, ic, regime}, ...]
    # factor_ics: {factor: {N: {regime: [ic, ...]}}}
    factor_ics: dict[str, dict[int, dict[str, list[float]]]] = {}

    for f in fields:
        factor_ics[f] = {N: {} for N in FORWARD_N}

    skipped = 0
    for di, td in enumerate(dates):
        # 检查：这一天之后要有足够的日期算 forward return
        max_N = max(FORWARD_N)
        if di + max_N >= len(dates):
            break

        # 收集当日横截面：{ts_code: {factor_val, fwd_returns}}
        cross: dict[str, dict] = {}
        for code, rows in stock_data.items():
            idx = _date_index(rows, td)
            if idx is None or idx < 20:  # 前20天用于因子计算预热
                continue
            # 检查是否有足够的未来数据
            if idx + max_N >= len(rows):
                continue

            row = rows[idx]
            fwd = {}
            for N in FORWARD_N:
                if idx + N < len(rows) and rows[idx]["close"] > 0:
                    fwd[N] = (rows[idx + N]["close"] - rows[idx]["close"]) / rows[idx]["close"]

            if len(fwd) < len(FORWARD_N):
                continue

            cross[code] = {
                "row": row,
                "idx": idx,
                "rows": rows,
                "fwd": fwd,
            }

        if len(cross) < MIN_CROSS_SECTION:
            skipped += 1
            continue

        regime = regime_cache.get(td, "unknown")

        for f in fields:
            f_vals: list[float] = []
            fwd_by_N: dict[int, list[float]] = {N: [] for N in FORWARD_N}
            codes_sorted = list(cross.keys())

            for code in codes_sorted:
                item = cross[code]
                fv = _compute_factor_at(item["rows"], item["idx"], f)
                if fv is None:
                    continue
                f_vals.append(fv)
                for N in FORWARD_N:
                    if N in item["fwd"]:
                        fwd_by_N[N].append(item["fwd"][N])

            if len(f_vals) < MIN_CROSS_SECTION:
                continue

            for N in FORWARD_N:
                if len(fwd_by_N[N]) != len(f_vals):
                    continue
                ic = _spearman(f_vals, fwd_by_N[N])
                if not math.isnan(ic):
                    ic_rows.append({"date": td, "factor": f, "N": N, "ic": round(ic, 6), "regime": regime})
                    factor_ics[f][N].setdefault(regime, []).append(ic)

    if skipped > 0:
        logger.info(f"Skipped {skipped}/{len(dates)} dates (< {MIN_CROSS_SECTION} stocks)")

    # ── 汇总（全窗口 + 按 regime 分段） ──
    summary = []
    all_regimes = sorted(set(r for row in ic_rows for r in [row["regime"]]))

    for f in fields:
        entry: dict = {"pool": pool_name, "factor": f}
        # 全窗口
        for N in FORWARD_N:
            all_ics = []
            for regime_ics in factor_ics[f][N].values():
                all_ics.extend(regime_ics)
            if all_ics:
                mean_ic = sum(all_ics) / len(all_ics)
                std_ic = _std(all_ics, mean_ic)
                ic_ir = round(mean_ic / std_ic, 4) if std_ic > 0 else 0.0
                pos_ratio = round(sum(1 for v in all_ics if v > 0) / len(all_ics), 4)
                entry[f"IC_{N}d_mean"] = round(mean_ic, 4)
                entry[f"IC_{N}d_ir"] = ic_ir
                entry[f"IC_{N}d_pos"] = pos_ratio
            else:
                entry[f"IC_{N}d_mean"] = None
                entry[f"IC_{N}d_ir"] = None
                entry[f"IC_{N}d_pos"] = None

        # 按 regime
        for regime in all_regimes:
            for N in FORWARD_N:
                ics = factor_ics[f][N].get(regime, [])
                if ics and len(ics) >= 3:
                    mean_ic = sum(ics) / len(ics)
                    entry[f"IC_{N}d_{regime}"] = round(mean_ic, 4)
                else:
                    entry[f"IC_{N}d_{regime}"] = None
        summary.append(entry)

    # ── 写 CSV ──
    ts = _now()
    seq_path = _OUT_DIR / f"ic_sequence_{pool_name}_{ts}.csv"
    with open(seq_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "factor", "N", "ic", "regime"])
        w.writeheader()
        w.writerows(ic_rows)
    logger.info(f"IC sequence → {seq_path} ({len(ic_rows)} rows)")

    sum_path = _OUT_DIR / f"ic_summary_{pool_name}_{ts}.csv"
    with open(sum_path, "w", newline="", encoding="utf-8-sig") as fh:
        base_fields = ["pool", "factor"] + [f"IC_{N}d_{m}" for N in FORWARD_N for m in ["mean", "ir", "pos"]]
        regime_fields = [f"IC_{N}d_{r}" for r in all_regimes for N in FORWARD_N]
        fields_sum = base_fields + regime_fields
        w = csv.DictWriter(fh, fieldnames=fields_sum)
        w.writeheader()
        w.writerows(summary)
    logger.info(f"IC summary → {sum_path}")

    # ── 日志输出 ──
    print(f"\n{'='*80}")
    print(f"  Rank IC Summary — {pool_name}")
    print(f"{'='*80}")

    # 全窗口
    header = f"{'Factor':<20}" + "".join(f"  N={N:<4d}" for N in FORWARD_N)
    print("  [全窗口]")
    print("  " + header)
    print("  " + "-" * len(header))
    for s in summary:
        line = f"  {s['factor']:<18}"
        for N in FORWARD_N:
            v = s.get(f"IC_{N}d_mean")
            line += f"  {v:+.4f}" if v is not None else "     N/A"
        print(line)

    # 按 regime 分段
    for regime in all_regimes:
        rname = {"bull": "单边上涨", "bear": "单边下跌", "range": "震荡", "volatile": "高波动", "unknown": "未知"}.get(regime, regime)
        print(f"\n  [{rname}]")
        print("  " + f"{'Factor':<20}" + "".join(f"  N={N:<4d}" for N in FORWARD_N))
        print("  " + "-" * len(header))
        for s in summary:
            line = f"  {s['factor']:<18}"
            for N in FORWARD_N:
                v = s.get(f"IC_{N}d_{regime}")
                line += f"  {v:+.4f}" if v is not None else "     N/A"
            print(line)

    print(f"\nCSV: {seq_path}\n     {sum_path}")

    return {"summary": summary, "ic_rows": ic_rows, "seq_path": str(seq_path), "sum_path": str(sum_path)}


def _date_index(rows: list[dict], target_date: str) -> int | None:
    for i, r in enumerate(rows):
        if r["date"] == target_date:
            return i
    return None


def _compute_factor_at(rows: list[dict], idx: int, field: str) -> float | None:
    """在指定时间点计算单个因子值（使用截至 idx 的数据）。"""
    row = rows[idx]

    # 静态字段（无需历史）
    if field == "pct_chg":
        return row["pct_chg"]
    if field in ("pe", "pb", "total_mv", "turnover"):
        v = row.get(field)
        return float(v) if v is not None and v > 0 else None

    # 需要历史的字段：用截至 idx 的窗口计算
    closes = [r["close"] for r in rows[: idx + 1]]
    volumes = [r["volume"] for r in rows[: idx + 1]]
    i = idx

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
        if i >= 20:
            avg_vol = sum(volumes[i - 19 : i + 1]) / 20
            if avg_vol > 0:
                return volumes[i] / avg_vol
    elif field == "dist_from_low":
        if i >= 20:
            lows = [closes[j] for j in range(i - 19, i + 1)]
            min_low = min(lows)
            if min_low > 0:
                return (closes[i] - min_low) / min_low * 100
    elif field == "ma_slope":
        if i >= 11:
            ma10 = sum(closes[i - 9 : i + 1]) / 10
            ma10_2 = sum(closes[i - 11 : i - 1]) / 10
            if ma10_2 > 0:
                return (ma10 - ma10_2) / ma10_2 * 100
    elif field == "drawdown":
        if i >= 20:
            highs = [closes[j] for j in range(i - 19, i + 1)]
            peak = max(highs)
            if peak > 0:
                return (peak - closes[i]) / peak * 100
    elif field == "chg3_recent":
        if i >= 3 and closes[i - 3] > 0:
            return (closes[i] - closes[i - 3]) / closes[i - 3] * 100

    return None


def _std(vals: list[float], mean: float) -> float:
    if len(vals) < 2:
        return 0.0
    return (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


# ── 数据同步 ──

async def _sync_daily_basic(trade_dates: list[str] | None = None):
    """补拉 daily_basic 数据。如果指定 trade_dates，只补这些日期的缺失。
    否则从 DB 最新日期补到昨天。"""
    from app.services.tushare_client import get_daily_basic
    from app.core.database import async_session as get_session

    async with get_session() as session:
        if trade_dates:
            placeholders = ",".join(f":d{i}" for i in range(len(trade_dates)))
            params = {f"d{i}": d for i, d in enumerate(trade_dates)}
            r = await session.execute(
                text(f"SELECT DISTINCT trade_date FROM daily_basic WHERE trade_date IN ({placeholders})"),
                params,
            )
            existing = {row[0] for row in r.fetchall()}
            missing_dates = [d for d in trade_dates if d not in existing]
        else:
            r = await session.execute(text("SELECT MAX(trade_date) FROM daily_basic"))
            last_date = r.scalar()
            if last_date:
                last_dt = datetime.strptime(last_date, "%Y%m%d").date()
            else:
                last_dt = date.today() - timedelta(days=180)
            today = date.today()
            if last_dt >= today - timedelta(days=1):
                logger.info(f"daily_basic up to date (last: {last_dt})")
                return
            missing_dates = []
            d = last_dt + timedelta(days=1)
            while d < today:
                missing_dates.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)

    if not missing_dates:
        logger.info("daily_basic: all dates covered")
        return

    logger.info(f"Syncing daily_basic: {len(missing_dates)} days ({missing_dates[0]}~{missing_dates[-1]})")
    async with get_session() as session:
        for td in missing_dates:
            try:
                rows = await get_daily_basic(trade_date=td)
                if rows:
                    for row in rows:
                        await session.execute(text("""
                            INSERT OR IGNORE INTO daily_basic
                                (ts_code, trade_date, turnover_rate, pe, pb, total_mv, circ_mv)
                            VALUES (:ts_code, :trade_date, :turnover_rate, :pe, :pb, :total_mv, :circ_mv)
                        """), {
                            "ts_code": row.get("ts_code", ""),
                            "trade_date": str(row.get("trade_date", td)),
                            "turnover_rate": float(row.get("turnover_rate", 0) or 0),
                            "pe": float(row.get("pe", 0) or 0),
                            "pb": float(row.get("pb", 0) or 0),
                            "total_mv": float(row.get("total_mv", 0) or 0),
                            "circ_mv": float(row.get("circ_mv", 0) or 0),
                        })
                    await session.commit()
                    logger.debug(f"daily_basic {td}: {len(rows)} rows")
            except Exception as e:
                logger.warning(f"daily_basic sync fail {td}: {e}")
            await asyncio.sleep(1.5)


async def _sync_stock_daily(need_days: int):
    """补拉缺失的 stock_daily（每日全量接口）。"""
    from app.services.tushare_client import get_all_daily
    from app.core.database import async_session as get_session

    async with get_session() as session:
        r = await session.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
        last_date = r.scalar()

    if last_date:
        last_dt = datetime.strptime(last_date, "%Y%m%d").date()
    else:
        last_dt = date.today() - timedelta(days=need_days)

    today = date.today()
    if last_dt >= today - timedelta(days=1):
        logger.info(f"stock_daily up to date (last: {last_dt})")
        return

    missing_dates = []
    d = last_dt + timedelta(days=1)
    while d < today:
        missing_dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    logger.info(f"Syncing stock_daily: {len(missing_dates)} days")
    async with get_session() as session:
        for td in missing_dates:
            try:
                rows = await get_all_daily(td)
                if rows:
                    for row in rows:
                        await session.execute(text("""
                            INSERT OR IGNORE INTO stock_daily
                                (ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, volume, amount)
                            VALUES (:ts_code, :trade_date, :open, :high, :low, :close, :pre_close, :change, :pct_chg, :volume, :amount)
                        """), {
                            "ts_code": row.get("ts_code", ""),
                            "trade_date": str(row.get("trade_date", td)),
                            "open": float(row.get("open", 0) or 0),
                            "high": float(row.get("high", 0) or 0),
                            "low": float(row.get("low", 0) or 0),
                            "close": float(row.get("close", 0) or 0),
                            "pre_close": float(row.get("pre_close", 0) or 0),
                            "change": float(row.get("change", 0) or 0),
                            "pct_chg": float(row.get("pct_chg", 0) or 0),
                            "volume": float(row.get("vol", 0) or 0),
                            "amount": float(row.get("amount", 0) or 0),
                        })
                    await session.commit()
                    logger.debug(f"stock_daily {td}: {len(rows)} stocks")
            except Exception as e:
                logger.warning(f"stock_daily sync fail {td}: {e}")
            await asyncio.sleep(1.0)


# ── CLI ──

async def main():
    parser = argparse.ArgumentParser(description="因子 IC 分析")
    parser.add_argument("--pool", default="all", help="池名或 'all'（默认 all）")
    parser.add_argument("--lookback", type=int, default=120, help="回溯交易日数（默认 120）")
    parser.add_argument("--sync", action="store_true", help="分析前先从 Tushare 补数据")
    args = parser.parse_args()

    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        fw = json.load(f)

    pools = list(fw.keys()) if args.pool == "all" else [args.pool]
    unknown = [p for p in pools if p not in fw]
    if unknown:
        logger.error(f"Unknown pools: {unknown}. Available: {list(fw.keys())}")
        return

    t0 = time.perf_counter()
    for p in pools:
        await analyze_pool(p, args.lookback, sync=args.sync)
    elapsed = time.perf_counter() - t0
    logger.info(f"Total: {elapsed:.1f}s for {len(pools)} pool(s)")


if __name__ == "__main__":
    asyncio.run(main())
