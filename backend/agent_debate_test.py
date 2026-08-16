"""trading agent（多空辩论）有效性验证。

回答两层问题：
1. 【天花板】辩论的输入（量比 / 均线 / MACD 等规则信号）有没有前向 alpha？—— 无 LLM 成本。
2. 【agent 本身】辩论 LLM 输出的 direction/confidence 有没有超过天花板？—— 需 --llm 开启。

方法论与既有 *_test.py 完全一致（无前视、ATR 标准化 0.5σ、随机基准）：
- 只用 t 及之前的数据算信号（window = daily[:t+1]）
- 标签 = (close[t+fwd]-close[t])/close[t] / (atr_pct*sqrt(fwd))，>0.5σ 记涨、<-0.5σ 记跌
- 随机基准 = 任意一天未来涨/跌超 0.5σ 的概率

用法:
  python agent_debate_test.py [sample_size] [forward_days] [--llm N]

默认跑第 1 层（纯规则，无 LLM，秒级）。
--llm N: 额外对最多 N 个历史点调用多空辩论 LLM，测方向命中率（消耗 DeepSeek token）。
"""
from __future__ import annotations

import asyncio
import math
import random
import sys

sys.path.insert(0, 'backend')

from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct

THRESHOLD = 0.5


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _build_indicators(window):
    """从日线窗口构建辩论用的紧凑指标 dict（与诊股 quant 同源）。"""
    from app.services.factor_lib import macd, rsi, kdj, bollinger, sma, atr
    closes = [float(d["close"] or 0) for d in window]
    highs = [float(d["high"] or 0) for d in window]
    lows = [float(d["low"] or 0) for d in window]
    vols = [float(d["volume"] or 0) for d in window]

    m = macd(closes)
    r = rsi(closes)
    k = kdj(highs, lows, closes)
    b = bollinger(closes)
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    a = atr(highs, lows, closes, 14)

    last = len(closes) - 1
    vol_ratio = (vols[-1] / _mean(vols[-20:])) if len(vols) >= 20 and _mean(vols[-20:]) > 0 else 1.0

    def _g(lst, i=last):
        return round(float(lst[i]), 4) if lst and i < len(lst) and lst[i] is not None else None

    return {
        "close": round(closes[-1], 2),
        "indicators": {
            "macd": {"dif": _g(m["dif"]), "dea": _g(m["dea"]), "bar": _g(m["bar"])},
            "rsi": _g(r),
            "kdj": {"k": _g(k["k"]), "d": _g(k["d"]), "j": _g(k["j"])},
            "boll": {"mid": _g(b["mid"]), "upper": _g(b["upper"]), "lower": _g(b["lower"])},
            "ma5": _g(ma5), "ma10": _g(ma10), "ma20": _g(ma20),
            "atr": _g(a),
            "vol_ratio": round(vol_ratio, 2),
        },
    }


async def run(sample_size: int, fwd: int, llm_n: int = 0):
    codes = await _sample_stocks(sample_size)

    # ── 第 1 层：规则信号天花板 ──
    base_up = [0, 0]
    base_down = [0, 0]
    vol_gt2_up = [0, 0]
    vol_gt2_down = [0, 0]
    ma_up = [0, 0]
    ma_down = [0, 0]

    candidates = []  # (code, window, daily, t) 供第 2 层 LLM 抽样

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 60:
            continue
        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 30:
                continue
            close = float(window[-1]["close"] or 0)
            if close <= 0:
                continue

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

            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            ma5 = _mean([float(d["close"] or 0) for d in window[-5:]])
            ma20 = _mean([float(d["close"] or 0) for d in window[-20:]])

            if vol_ratio > 2.0:
                vol_gt2_up[1] += 1
                vol_gt2_down[1] += 1
                if is_up:
                    vol_gt2_up[0] += 1
                if is_down:
                    vol_gt2_down[0] += 1

            if ma5 > ma20:
                ma_up[1] += 1
                if is_up:
                    ma_up[0] += 1
            else:
                ma_down[1] += 1
                if is_down:
                    ma_down[0] += 1

            if llm_n > 0:
                candidates.append((code, window, daily, t))

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    print(f"\n=== trading agent 有效性验证 (样本{len(codes)}股, 前向{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 涨 {pct(base_up)}%  跌 {pct(base_down)}%   (总{base_up[1]}样本)")

    print(f"\n── 第 1 层：辩论输入的天花板（规则信号，无 LLM）──")
    print(f"{'信号':<20}{'触发':<10}{'命中率':<10}{'vs基准':<10}")
    rows = [
        ("量比>2 → 涨", vol_gt2_up, base_up),
        ("量比>2 → 跌", vol_gt2_down, base_down),
        ("MA5>MA20 → 涨", ma_up, base_up),
        ("MA5<MA20 → 跌", ma_down, base_down),
    ]
    for name, b, base in rows:
        rate = pct(b)
        base_rate = pct(base)
        diff = round(rate - base_rate, 1)
        print(f"{name:<20}{b[1]:<10}{rate:<10}{'+' if diff >= 0 else ''}{diff}pp")

    # ── 第 2 层：LLM 方向命中率 ──
    if llm_n > 0 and candidates:
        from app.services.ai_analysis import analyze_stock_debate
        sample_pts = random.sample(candidates, min(llm_n, len(candidates)))
        llm_up = [0, 0]
        llm_down = [0, 0]
        llm_neutral = [0, 0]
        conf_buckets = {}
        print(f"\n── 第 2 层：辩论 LLM 方向命中率（{len(sample_pts)} 点，消耗 DeepSeek）──")
        for i, (code, window, daily, t) in enumerate(sample_pts):
            indicators = _build_indicators(window)
            close = float(window[-1]["close"] or 1)
            future_ret = (_safe_close(daily, t + fwd) - close) / close
            ret_norm = future_ret / (_atr_pct(daily, t) * math.sqrt(fwd))
            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD
            try:
                result = await analyze_stock_debate(
                    stock_code=code, stock_name=code,
                    indicators_json=indicators, use_cache=False,
                )
                rating = result["rating"]
                direction = rating.get("direction", "neutral")
                conf = int(rating.get("confidence", 1) or 1)
            except Exception as e:
                print(f"  [{i + 1}] {code} LLM 失败: {e}")
                continue

            if direction == "bullish":
                llm_up[1] += 1
                if is_up:
                    llm_up[0] += 1
            elif direction == "bearish":
                llm_down[1] += 1
                if is_down:
                    llm_down[0] += 1
            else:
                llm_neutral[1] += 1

            conf_buckets.setdefault(conf, [0, 0])
            conf_buckets[conf][1] += 1
            if (direction == "bullish" and is_up) or (direction == "bearish" and is_down):
                conf_buckets[conf][0] += 1

            if (i + 1) % 10 == 0:
                print(f"  ... 已跑 {i + 1}/{len(sample_pts)}")

        print(f"\n辩论方向命中率:")
        print(f"  bullish → 涨: {pct(llm_up)}% (n={llm_up[1]}, 基准 {pct(base_up)}%)")
        print(f"  bearish → 跌: {pct(llm_down)}% (n={llm_down[1]}, 基准 {pct(base_down)}%)")
        print(f"  neutral 占比: {round(100.0 * llm_neutral[1] / max(1, len(sample_pts)), 1)}%")
        print(f"\n按置信度分层命中率 (方向对=命中):")
        for conf in sorted(conf_buckets):
            b = conf_buckets[conf]
            print(f"  conf={conf}: {pct(b)}% (n={b[1]})")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    llm_n = 0
    if "--llm" in sys.argv:
        idx = sys.argv.index("--llm")
        if idx + 1 < len(sys.argv):
            llm_n = int(sys.argv[idx + 1])
    asyncio.run(run(sample, fwd, llm_n))
