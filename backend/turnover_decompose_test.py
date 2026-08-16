"""换手率 vs 量比 独立性分解 — 2×2 组合的前向「看跌」命中率。

回答两个问题：
1. 换手率突增是不是量比>2 的"另一种表达"，还是有独立 edge？
2. 量比>2 + 换手率突增 双触发，看跌命中率是否比单因子更高？

2×2 分解（均看「未来5日跌超0.5σ」的命中率 vs 随机基准）：
- 量比>2 且 换手率突增（双触发）
- 量比>2 且 换手率不高（量比独立）
- 换手率突增 且 量比≤2（换手率独立 ← 关键独立性检验）
- 换手率不高 且 量比≤2（双无，对照组）

用法: python turnover_decompose_test.py [sample_size] [forward_days]
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
            text(
                "SELECT trade_date, total_mv, circ_mv, turnover_rate "
                "FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = {
            "total_mv": float(row["total_mv"] or 0),
            "circ_mv": float(row["circ_mv"] or 0),
            "turnover_rate": float(row["turnover_rate"] or 0),
        }
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    base_down = [0, 0]  # 随机基准看跌
    buckets = {
        "量比>2 且 换手率突增(双触发)": [0, 0],
        "量比>2 且 换手率不高": [0, 0],
        "换手率突增 且 量比≤2(独立性)": [0, 0],
        "换手率不高 且 量比≤2(对照组)": [0, 0],
    }

    usable = 0
    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 60:
            continue
        basic = await _load_daily_basic(code)
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
            is_down = ret_norm < -THRESHOLD

            base_down[1] += 1
            if is_down:
                base_down[0] += 1

            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0
            vol_high = vol_ratio > 2.0

            b = basic.get(td)
            if b is None:
                continue
            trs = [basic[d["trade_date"]]["turnover_rate"]
                   for d in window[-60:]
                   if d["trade_date"] in basic and basic[d["trade_date"]]["turnover_rate"] > 0]
            turn_high = (
                len(trs) >= 30 and b["turnover_rate"] > 0
                and b["turnover_rate"] >= sorted(trs)[int(0.9 * len(trs))]
            )

            if vol_high and turn_high:
                key = "量比>2 且 换手率突增(双触发)"
            elif vol_high:
                key = "量比>2 且 换手率不高"
            elif turn_high:
                key = "换手率突增 且 量比≤2(独立性)"
            else:
                key = "换手率不高 且 量比≤2(对照组)"

            buckets[key][1] += 1
            if is_down:
                buckets[key][0] += 1

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    base_rate = pct(base_down)
    print(f"\n=== 换手率×量比 2×2 分解 (样本{usable}股, 前向{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准看跌: {base_rate}%   (总{base_down[1]}样本)")

    print(f"\n{'组合':<30}{'触发':<10}{'看跌命中率':<12}{'vs基准':<10}")
    for key, bb in buckets.items():
        rate = pct(bb)
        diff = round(rate - base_rate, 1)
        print(f"{key:<30}{bb[1]:<10}{rate:<12}{'+' if diff >= 0 else ''}{diff}pp")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
