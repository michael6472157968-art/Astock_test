"""共振命中率测试——五眼共识的 alpha 验证。

回答：几眼同时喊买/卖时，未来走强的概率是否超过随机基准？

- 随机基准: 任意一天买入，未来涨超 0.5σ 的概率（约 31%）
- 共振信号: N 眼同时喊 buy/sell 时的命中率，N=1..5
- 标签: ATR标准化 (ret / (atr_pct * sqrt(fwd)))，阈值 0.5σ

用法: python resonance_test.py [sample_size] [forward_days]
"""
from __future__ import annotations

import asyncio
import math
import sys

sys.path.insert(0, 'backend')

from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct
from app.services.multi_eye import candle_eye, indicator_eye, chan_eye, wave_eye, gann_eye

EYE_FUNCS = {
    "candle": candle_eye, "indicator": indicator_eye,
    "chan": chan_eye, "wave": wave_eye, "gann": gann_eye,
}

THRESHOLD = 0.5  # 0.5 个标准差


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # buy_buckets[n] = [hit, total]，n = 同时喊 buy 的眼数
    buy_buckets = {n: [0, 0] for n in range(6)}
    sell_buckets = {n: [0, 0] for n in range(6)}
    # 趋势共振: n 眼同看 up / down
    up_buckets = {n: [0, 0] for n in range(6)}
    down_buckets = {n: [0, 0] for n in range(6)}
    # 随机基准
    base_up = [0, 0]
    base_down = [0, 0]

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

            # 标签
            future_ret = (_safe_close(daily, t + fwd) - _safe_close(daily, t)) / _safe_close(daily, t)
            atr_pct = _atr_pct(daily, t)
            if atr_pct > 0:
                ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            else:
                continue

            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD

            # 随机基准
            base_up[1] += 1
            base_down[1] += 1
            if is_up:
                base_up[0] += 1
            if is_down:
                base_down[0] += 1

            n_buy = sum(1 for v in verdicts.values() if v.signal == "buy")
            n_sell = sum(1 for v in verdicts.values() if v.signal == "sell")
            n_up = sum(1 for v in verdicts.values() if v.trend == "up")
            n_down = sum(1 for v in verdicts.values() if v.trend == "down")

            buy_buckets[n_buy][1] += 1
            sell_buckets[n_sell][1] += 1
            up_buckets[n_up][1] += 1
            down_buckets[n_down][1] += 1

            if is_up:
                buy_buckets[n_buy][0] += 1
                up_buckets[n_up][0] += 1
            if is_down:
                sell_buckets[n_sell][0] += 1
                down_buckets[n_down][0] += 1

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    print(f"\n=== 共振命中率 (样本{len(codes)}股, 窗口{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 涨 {pct(base_up)}%  跌 {pct(base_down)}%")

    print(f"\n--- 信号共振 (signal) ---")
    print(f"{'眼数':<6}{'喊买':<10}{'命中率':<10}{'喊卖':<10}{'命中率':<10}")
    for n in range(6):
        print(f"{n:<6}{buy_buckets[n][1]:<10}{pct(buy_buckets[n]):<10}"
              f"{sell_buckets[n][1]:<10}{pct(sell_buckets[n]):<10}")

    print(f"\n--- 趋势共振 (trend) ---")
    print(f"{'眼数':<6}{'看涨':<10}{'命中率':<10}{'看跌':<10}{'命中率':<10}")
    for n in range(6):
        print(f"{n:<6}{up_buckets[n][1]:<10}{pct(up_buckets[n]):<10}"
              f"{down_buckets[n][1]:<10}{pct(down_buckets[n]):<10}")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    asyncio.run(run(sample, fwd))
