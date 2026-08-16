"""阈值敏感度测试——回答"是不是阈值把趋势样本抹掉了"。

三个角度绕过二元阈值陷阱：
1. 多档阈值 (0.2~1.0σ) 下 signal 命中率 vs 随机基准的超额
2. 连续未来收益均值：眼睛喊 buy/sell 的子集 vs 全样本基准
3. 眼睛喊 buy 子集的未来收益分布（分位数）

用法: python threshold_test.py [sample_size] [fwd]
"""
from __future__ import annotations

import asyncio
import math
import sys

sys.path.insert(0, '.')

from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct
from app.services.multi_eye import candle_eye, indicator_eye, chan_eye, wave_eye, gann_eye

EYE_FUNCS = {
    "candle": candle_eye, "indicator": indicator_eye,
    "chan": chan_eye, "wave": wave_eye, "gann": gann_eye,
}


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 收集所有 (n_buy, n_sell, ret_norm) 样本
    samples = []  # dict: n_buy, n_sell, ret_norm

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 40:
            continue
        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 30:
                continue
            verdicts = {}
            for name, fn in EYE_FUNCS.items():
                try:
                    verdicts[name] = fn(window)
                except Exception:
                    pass
            if len(verdicts) < 5:
                continue
            future_ret = (_safe_close(daily, t + fwd) - _safe_close(daily, t)) / _safe_close(daily, t)
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            n_buy = sum(1 for v in verdicts.values() if v.signal == "buy")
            n_sell = sum(1 for v in verdicts.values() if v.signal == "sell")
            samples.append({"n_buy": n_buy, "n_sell": n_sell, "ret_norm": ret_norm})

    total = len(samples)
    if total == 0:
        print("无样本")
        return

    all_rets = [s["ret_norm"] for s in samples]
    all_mean = sum(all_rets) / total

    print(f"\n=== 阈值敏感度 & 连续收益 (样本{len(codes)}股, 窗口{fwd}日, 观测{total}条) ===")
    print(f"全样本未来收益均值 (ret/σ): {all_mean:+.4f}")

    # 角度1: 多档阈值
    print(f"\n--- 角度1: 多档阈值下 signal-buy 命中率 vs 随机基准 ---")
    print(f"{'阈值':<8}{'随机涨概率':<12}{'buy≥1命中':<12}{'buy≥3命中':<12}{'超额(≥3)':<10}")
    for th in [0.2, 0.3, 0.4, 0.5, 0.7, 1.0]:
        base_up = sum(1 for r in all_rets if r > th) / total
        b1 = [s for s in samples if s["n_buy"] >= 1]
        b3 = [s for s in samples if s["n_buy"] >= 3]
        h1 = sum(1 for s in b1 if s["ret_norm"] > th) / len(b1) if b1 else 0
        h3 = sum(1 for s in b3 if s["ret_norm"] > th) / len(b3) if b3 else 0
        print(f"{th:<8.2f}{base_up*100:<12.1f}{h1*100:<12.1f}{h3*100:<12.1f}{(h3-base_up)*100:+.1f}pp")

    # 角度2: 连续收益均值对比
    print(f"\n--- 角度2: 连续未来收益均值 (眼数分档) ---")
    print(f"{'buy眼数':<10}{'样本数':<10}{'收益均值':<12}{'vs全局':<10}")
    for n in range(6):
        sub = [s["ret_norm"] for s in samples if s["n_buy"] == n]
        if not sub:
            continue
        m = sum(sub) / len(sub)
        print(f"{n:<10}{len(sub):<10}{m:+.4f}    {(m-all_mean)*100:+.2f}pp")

    # 角度3: buy≥3 子集的收益分布
    b3 = sorted([s["ret_norm"] for s in samples if s["n_buy"] >= 3])
    if b3:
        def q(p):
            return b3[int(p * (len(b3) - 1))]
        print(f"\n--- 角度3: buy≥3 子集未来收益分布 (n={len(b3)}) ---")
        print(f"P10={q(0.1):+.3f}  P25={q(0.25):+.3f}  中位={q(0.5):+.3f}  P75={q(0.75):+.3f}  P90={q(0.9):+.3f}")
        print(f"全局中位: {sorted(all_rets)[int(0.5*(total-1))]:+.3f}")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    asyncio.run(run(sample, fwd))
