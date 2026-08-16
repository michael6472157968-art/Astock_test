"""量价背离度 alpha 确认 — 对比日线量比基线。

发现（minute_factor_test.py 意外挖到）：量价背离度 = close 近20日分位 - vol 近20日分位（纯日线），
高值（价高量低 = 缩量上涨）→ 看涨 +7.6pp，2×2 分解独立于日线量比。

本脚本正式对比两个信号，纯日线、无需分钟数据、可跑大样本 + 多 forward：

- 信号A（基线，已知 alpha）：日线量比>2 → 看跌
- 信号B（新 alpha）：量价背离度 top10%（价高量低）→ 看涨
- 2×2 分解 + 合并信号的方向正确率

用法: cd backend && PYTHONIOENCODING=utf-8 python divergence_confirm.py [sample_size] [forward_days]
"""
from __future__ import annotations

import asyncio
import math
import sys

sys.path.insert(0, ".")

from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct

THRESHOLD = 0.5


def _pct_rank_value(values):
    if not values:
        return 0.5
    v = values[-1]
    return sum(1 for x in values if x <= v) / len(values)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 收集: (vol_ratio, divergence, is_up, is_down)
    samples: list[tuple] = []
    base_up = [0, 0]
    base_down = [0, 0]
    usable = 0

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 40:
            continue
        usable += 1

        for t in range(20, len(daily) - fwd):
            close = float(daily[t].get("close", 0) or 0)
            if close <= 0:
                continue
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            future_ret = (_safe_close(daily, t + fwd) - close) / close
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD

            base_up[1] += 1
            base_down[1] += 1
            if is_up:
                base_up[0] += 1
            if is_down:
                base_down[0] += 1

            # 日线量比
            vols20 = [float(daily[i]["volume"] or 0) for i in range(t - 19, t + 1)]
            avg_vol = sum(vols20) / 20 if vols20 else 0.0
            vol_ratio = (float(daily[t]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            # 量价背离度 = close 分位 - vol 分位
            closes20 = [float(daily[i]["close"] or 0) for i in range(t - 19, t + 1)]
            close_rank = _pct_rank_value(closes20)
            vol_rank = _pct_rank_value(vols20)
            divergence = close_rank - vol_rank

            samples.append((vol_ratio, divergence, is_up, is_down))

    def pct(hit, total):
        return round(100.0 * hit / total, 1) if total > 0 else 0.0

    base_up_r = pct(base_up[0], base_up[1])
    base_down_r = pct(base_down[0], base_down[1])
    print(f"\n=== 量价背离度 vs 日线量比 (样本{usable}股, 前向{fwd}日, 总{base_up[1]}样本) ===")
    print(f"随机基准: 涨 {base_up_r}%  跌 {base_down_r}%")

    # 背离度 top10% 阈值
    divs = sorted(s[1] for s in samples)
    div_hi = divs[int(0.9 * len(divs))]

    # 信号A: 量比>2 看跌
    a_grp = [s for s in samples if s[0] > 2.0]
    a_down = pct(sum(1 for s in a_grp if s[3]), len(a_grp))
    a_up = pct(sum(1 for s in a_grp if s[2]), len(a_grp))

    # 信号B: 背离度高 看涨
    b_grp = [s for s in samples if s[1] >= div_hi]
    b_up = pct(sum(1 for s in b_grp if s[2]), len(b_grp))
    b_down = pct(sum(1 for s in b_grp if s[3]), len(b_grp))

    print(f"\n{'信号':<28}{'触发':<8}{'看涨':<10}{'看跌':<10}{'方向':<8}")
    print(f"{'A 日线量比>2 (看跌)':<28}{len(a_grp):<8}{a_up}%{'':<7}{a_down}%{'':<7}"
          f"{'看跌+' + str(round(a_down - base_down_r, 1)) + 'pp':<8}")
    print(f"{'B 量价背离度高 (看涨)':<28}{len(b_grp):<8}{b_up}%{'':<7}{b_down}%{'':<7}"
          f"{'看涨+' + str(round(b_up - base_up_r, 1)) + 'pp':<8}")

    # 2×2 分解
    print(f"\n── 2×2 分解 (量比>2 × 背离度 top10%) ──")
    print(f"{'':<24}{'n':<7}{'看涨':<9}{'看跌':<9}")
    cells = [
        ("放量>2 且 背离高", lambda s: s[0] > 2.0 and s[1] >= div_hi),
        ("放量>2 且 背离低", lambda s: s[0] > 2.0 and s[1] < div_hi),
        ("缩量≤2 且 背离高", lambda s: s[0] <= 2.0 and s[1] >= div_hi),
        ("缩量≤2 且 背离低", lambda s: s[0] <= 2.0 and s[1] < div_hi),
    ]
    for label, cond in cells:
        grp = [s for s in samples if cond(s)]
        if not grp:
            print(f"{label:<24}n=0")
            continue
        up = pct(sum(1 for s in grp if s[2]), len(grp))
        down = pct(sum(1 for s in grp if s[3]), len(grp))
        up_diff = round(up - base_up_r, 1)
        down_diff = round(down - base_down_r, 1)
        print(f"{label:<24}{len(grp):<7}{up}%{'(' + ('+' if up_diff >= 0 else '') + str(up_diff) + 'pp)':<9}"
              f"{down}%{'(' + ('+' if down_diff >= 0 else '') + str(down_diff) + 'pp)':<9}")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
