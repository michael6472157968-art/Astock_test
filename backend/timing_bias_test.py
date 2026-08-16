"""信号时点错位验证——放量/换手信号的"当日" vs "可执行" 拆解。

验证用户的猜想：短线反向信号是否因为"信号触发日已是主力出货日"，导致回测用T收盘价入场、
实际只能T+1开盘买入，从而高估了看跌 alpha。

三个维度：
1. 信号当日收益：量比>2/换手突增触发那天，股价本身涨还是跌？
   若当日 pct_chg<0 占比高 → 信号是"下跌中触发"，滞后而非预测。
2. 信号前一日收益：是否前一日已冲高(主力出货前的诱多)？
3. 可执行性：入场价从 T收盘 改为 T+1开盘，看跌 alpha 还剩多少？

用法: python timing_bias_test.py [sample_size] [forward_days]
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
                "SELECT trade_date, turnover_rate "
                "FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = float(row["turnover_rate"] or 0)
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 信号当日/前一日 收益分布 (针对量比>2 和 换手突增)
    # 结构: {signal: {day: [ret_sum, count]}}
    sig_day_ret = {
        "量比>2": {"prev1": [0.0, 0], "day0": [0.0, 0], "next1": [0.0, 0]},
        "换手突增": {"prev1": [0.0, 0], "day0": [0.0, 0], "next1": [0.0, 0]},
    }

    # 可执行性：T+1开盘入场 vs T收盘入场，看跌命中率对比
    # close_entry: 用T收盘价算 forward ret；open_entry: 用T+1开盘价算
    close_entry = {"量比>2": [0, 0], "换手突增": [0, 0]}
    open_entry = {"量比>2": [0, 0], "换手突增": [0, 0]}

    for code in codes:
        daily = await _load_daily_data_fast(code)
        turnover = await _load_daily_basic(code)
        if len(daily) < fwd + 60:
            continue

        for t in range(len(daily) - fwd - 1):  # 多留1天给 T+1 开盘
            window = daily[:t + 1]
            if len(window) < 60 or t + 1 >= len(daily):
                continue

            close_t = float(window[-1]["close"] or 0)
            if close_t <= 0:
                continue
            td = window[-1]["trade_date"]

            # 量比
            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            # 换手率突增
            trs = [turnover[d["trade_date"]] for d in window[-60:]
                   if d["trade_date"] in turnover and turnover[d["trade_date"]] > 0]
            tr_now = turnover.get(td, 0)
            is_turnover_surge = (len(trs) >= 30 and tr_now > 0
                                 and tr_now >= sorted(trs)[int(0.9 * len(trs))])

            signals = []
            if vol_ratio > 2.0:
                signals.append("量比>2")
            if is_turnover_surge:
                signals.append("换手突增")

            for sig in signals:
                # 当日/前一日/次日收益
                prev1_ret = _day_ret(daily, t - 1) if t >= 1 else None
                day0_ret = _day_ret(daily, t)
                next1_ret = _day_ret(daily, t + 1)

                if prev1_ret is not None:
                    sig_day_ret[sig]["prev1"][0] += prev1_ret
                    sig_day_ret[sig]["prev1"][1] += 1
                sig_day_ret[sig]["day0"][0] += day0_ret
                sig_day_ret[sig]["day0"][1] += 1
                sig_day_ret[sig]["next1"][0] += next1_ret
                sig_day_ret[sig]["next1"][1] += 1

                # 可执行性对比
                atr_pct = _atr_pct(daily, t)
                if atr_pct <= 0:
                    continue
                # T收盘入场
                fwd_close = _safe_close(daily, t + fwd)
                if fwd_close > 0:
                    ret_close = (fwd_close - close_t) / close_t
                    ret_norm_close = ret_close / (atr_pct * math.sqrt(fwd))
                    close_entry[sig][1] += 1
                    if ret_norm_close < -THRESHOLD:  # 看跌命中
                        close_entry[sig][0] += 1
                # T+1 开盘入场
                open_t1 = float(daily[t + 1].get("open", 0) or 0)
                if open_t1 > 0 and t + fwd < len(daily):
                    fwd_open = _safe_close(daily, t + fwd)
                    if fwd_open > 0:
                        ret_open = (fwd_open - open_t1) / open_t1
                        ret_norm_open = ret_open / (atr_pct * math.sqrt(fwd))
                        open_entry[sig][1] += 1
                        if ret_norm_open < -THRESHOLD:
                            open_entry[sig][0] += 1

    print(f"\n=== 信号时点拆解 (样本{len(codes)}股, 窗口{fwd}日) ===")

    print("\n── 1. 信号触发时的当日/前后收益 (平均收益率%) ──")
    print(f"{'信号':<12}{'前一日':<12}{'当日(day0)':<14}{'次日(day1)':<14}{'样本':<10}")
    for sig, days in sig_day_ret.items():
        def avg(d):
            return round(100.0 * d[0] / d[1], 2) if d[1] > 0 else 0.0
        print(f"{sig:<12}{avg(days['prev1']):<12}{avg(days['day0']):<14}"
              f"{avg(days['next1']):<14}{days['day0'][1]:<10}")

    print("\n── 2. 可执行性：T收盘入场 vs T+1开盘入场 (看跌命中率%) ──")
    print(f"{'信号':<12}{'T收盘入场':<14}{'T+1开盘入场':<14}{'衰减':<10}")
    for sig in ["量比>2", "换手突增"]:
        c = close_entry[sig]
        o = open_entry[sig]
        cr = round(100.0 * c[0] / c[1], 1) if c[1] > 0 else 0.0
        orate = round(100.0 * o[0] / o[1], 1) if o[1] > 0 else 0.0
        decay = round(cr - orate, 1)
        print(f"{sig:<12}{cr:<14}{orate:<14}{decay:<10}")


def _day_ret(daily, idx):
    """单日收益率 (close[idx] - close[idx-1]) / close[idx-1]。"""
    if idx < 1 or idx >= len(daily):
        return 0.0
    prev = float(daily[idx - 1].get("close", 0) or 0)
    cur = float(daily[idx].get("close", 0) or 0)
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
