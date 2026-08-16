"""超大单/大单拆解 — 「越大单越反转」验证。

学术结论（开源证券魏建榕）：A 股反转之力的微观来源是大单成交，「越大单越反转」——
超大单净买入 → 短期未来反而下跌（主力借大单出货）。

用 moneyflow_records 的 buy_elg_amount/sell_elg_amount（超大单）、
buy_lg_amount/sell_lg_amount（大单）验证，并与「主力净流入」对比（看是不是越细的单越有效）。

标签/基准与既有 *_test.py 一致：ATR 标准化 0.5σ、无前视、随机基准。

用法: python elg_order_test.py [sample_size] [forward_days]
"""
from __future__ import annotations

import asyncio
import math
import sys

sys.path.insert(0, 'backend')

from sqlalchemy import text

from app.core.database import async_session
from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct

THRESHOLD = 0.5


async def _load_daily_basic(ts_code: str) -> dict[str, dict]:
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT trade_date, circ_mv, turnover_rate FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = {
            "circ_mv": float(row["circ_mv"] or 0),
            "turnover_rate": float(row["turnover_rate"] or 0),
        }
    return out


async def _load_moneyflow(ts_code: str) -> dict[str, dict]:
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT trade_date, net_mf_amount, buy_elg_amount, sell_elg_amount, "
                "buy_lg_amount, sell_lg_amount FROM moneyflow_records "
                "WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = {
            "net_mf": float(row["net_mf_amount"] or 0),
            "net_elg": float(row["buy_elg_amount"] or 0) - float(row["sell_elg_amount"] or 0),
            "net_lg": float(row["buy_lg_amount"] or 0) - float(row["sell_lg_amount"] or 0),
        }
    return out


def _pct_rank(values):
    if not values:
        return 0.5
    v = values[-1]
    return sum(1 for x in values if x <= v) / len(values)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    base_up = [0, 0]
    base_down = [0, 0]
    buckets = {
        "超大单净流入top10%": [0, 0, 0, 0],
        "超大单净流出top10%": [0, 0, 0, 0],
        "大单净流入top10%": [0, 0, 0, 0],
        "主力净流入top10%": [0, 0, 0, 0],
    }

    usable = 0
    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 60:
            continue
        basic = await _load_daily_basic(code)
        mf = await _load_moneyflow(code)
        usable += 1

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 30:
                continue
            close = float(window[-1]["close"] or 0)
            if close <= 0:
                continue
            td = window[-1]["trade_date"]

            future_ret = (_safe_close(daily, t + fwd) - close) / close
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD

            base_up[1] += 1
            base_down[1] += 1
            if is_up:
                base_up[0] += 1
            if is_down:
                base_down[0] += 1

            # 近60日各净流入强度（净额/流通市值）
            elg_ratios, lg_ratios, mf_ratios = [], [], []
            for d in window[-60:]:
                dd = d["trade_date"]
                if dd in basic and dd in mf and basic[dd]["circ_mv"] > 0:
                    cmv = basic[dd]["circ_mv"]
                    elg_ratios.append(mf[dd]["net_elg"] / cmv)
                    lg_ratios.append(mf[dd]["net_lg"] / cmv)
                    mf_ratios.append(mf[dd]["net_mf"] / cmv)

            conds = {}
            if len(elg_ratios) >= 30:
                r_elg = _pct_rank(elg_ratios)
                r_lg = _pct_rank(lg_ratios)
                r_mf = _pct_rank(mf_ratios)
                conds["超大单净流入top10%"] = r_elg >= 0.9
                conds["超大单净流出top10%"] = r_elg <= 0.1
                conds["大单净流入top10%"] = r_lg >= 0.9
                conds["主力净流入top10%"] = r_mf >= 0.9
            else:
                for k in buckets:
                    conds[k] = False

            for name in buckets:
                if not conds[name]:
                    continue
                buckets[name][1] += 1
                buckets[name][3] += 1
                if is_up:
                    buckets[name][0] += 1
                if is_down:
                    buckets[name][2] += 1

    def pct(hit, total):
        return round(100.0 * hit / total, 1) if total > 0 else 0.0

    print(f"\n=== 超大单/大单拆解 (样本{usable}股, 前向{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 涨 {pct(base_up[0], base_up[1])}%  跌 {pct(base_down[0], base_down[1])}%   (总{base_up[1]}样本)")

    base_up_r = pct(base_up[0], base_up[1])
    base_down_r = pct(base_down[0], base_down[1])
    print(f"\n{'因子':<20}{'触发':<10}{'看涨':<10}{'看涨vs基准':<12}{'看跌':<10}{'看跌vs基准':<12}")
    for name, bb in buckets.items():
        up_r = pct(bb[0], bb[1])
        down_r = pct(bb[2], bb[3])
        up_diff = round(up_r - base_up_r, 1)
        down_diff = round(down_r - base_down_r, 1)
        print(f"{name:<20}{bb[1]:<10}{up_r:<10}{'+' if up_diff >= 0 else ''}{up_diff}pp{'':<6}"
              f"{down_r:<10}{'+' if down_diff >= 0 else ''}{down_diff}pp")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
