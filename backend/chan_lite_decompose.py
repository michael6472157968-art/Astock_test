"""分解 chan_lite 回测收益来源——为什么回测好看但 alpha 测试无 alpha。

对同一批股票，三种口径对比累计收益:
1. chan_lite 回测 (backtest_engine.run)
2. 买入持有 (buy & hold, 全期)
3. 随机信号回测 (同频次随机买卖, 用相同引擎成交逻辑)

若 chan_lite ≈ buy&hold ≈ 随机，则"好看"纯粹是 beta + 多头结构，无超额。

用法: python chan_lite_decompose.py [sample_size]
"""
from __future__ import annotations

import asyncio
import random
import sys

sys.path.insert(0, 'backend')

from app.services.calibration import _sample_stocks, _load_daily_data_fast
from app.services.backtest_engine import run as bt_run
from app.services.strategy_lib import s_chan_lite


def _buy_and_hold(daily: list[dict]) -> float:
    """全期买入持有收益率(%, 扣双边成本)。"""
    if len(daily) < 2:
        return 0.0
    c0 = float(daily[0]["close"])
    c1 = float(daily[-1]["close"])
    if c0 <= 0:
        return 0.0
    gross = (c1 - c0) / c0 * 100
    # 扣双边成本(佣金0.025%+印花税0.05%)约 0.125%
    return gross - 0.125


def _random_bt(daily: list[dict], n_signals: int, seed: int) -> float:
    """用相同引擎成交逻辑，随机买卖 n_signals 次，返回收益率。"""
    rng = random.Random(seed)
    n = len(daily)
    opens = [float(r.get("open", 0) or 0) for r in daily]
    closes = [float(r["close"]) for r in daily]

    # 生成随机信号: 在 [0, n-2] 随机选买入点，随后随机持有天数卖出
    cash = 100000.0
    shares = 0
    TAX, COMM, MINC = 0.0005, 0.00025, 5.0

    def cost(amt, action):
        c = max(amt * COMM, MINC)
        if action == "sell":
            c += amt * TAX
        return c

    i = 0
    made = 0
    while i < n - 1 and made < n_signals:
        if shares == 0:
            # 随机买入
            px = opens[i + 1]  # 次日开盘成交
            if px > 0:
                bs = int(cash * 0.98 / px / 100) * 100
                if bs <= 0:
                    bs = 10
                amt = bs * px
                cash -= amt + cost(amt, "buy")
                shares += bs
                made += 1
                # 随机持有 1~20 天
                hold = rng.randint(1, 20)
                i += hold
                continue
        else:
            # 随机卖出
            px = opens[i + 1]
            if px > 0:
                amt = shares * px
                cash += amt - cost(amt, "sell")
                shares = 0
                i += rng.randint(1, 10)
                continue
        i += 1

    # 期末平仓
    if shares > 0:
        amt = shares * closes[-1]
        cash += amt - cost(amt, "sell")
    return (cash - 100000) / 100000 * 100


async def main(sample: int):
    codes = await _sample_stocks(sample)
    chan_rets, bh_rets, rand_rets = [], [], []
    n_trades_list = []
    n_no_trade = 0

    for ci, code in enumerate(codes):
        daily = await _load_daily_data_fast(code)
        if len(daily) < 120:
            continue

        # 1. chan_lite 回测
        bt = bt_run(daily, strategy="chan_lite", initial_cash=100000)
        if "metrics" in bt:
            chan_rets.append(bt["metrics"]["total_return"])
            n_trades_list.append(bt["metrics"]["total_trades"])
            if bt["metrics"]["total_trades"] == 0:
                n_no_trade += 1
        else:
            chan_rets.append(0.0)
            n_trades_list.append(0)
            n_no_trade += 1

        # 2. 买入持有
        bh_rets.append(_buy_and_hold(daily))

        # 3. 随机信号 (同频次)
        n_sig = max(n_trades_list[-1] // 2, 1)
        rand_rets.append(_random_bt(daily, n_sig, seed=ci))

    def _stat(xs):
        if not xs:
            return (0, 0)
        xs = sorted(xs)
        return (round(sum(xs) / len(xs), 2), round(xs[len(xs) // 2], 2))

    print(f"\n=== chan_lite 回测收益分解 (样本 {len(codes)} 股, 2年) ===")
    print(f"{'口径':<20}{'平均收益':>12}{'中位收益':>12}")
    am, md = _stat(chan_rets)
    print(f"{'chan_lite 回测':<20}{am:>11}%{md:>12}%")
    am, md = _stat(bh_rets)
    print(f"{'买入持有':<20}{am:>11}%{md:>12}%")
    am, md = _stat(rand_rets)
    print(f"{'随机信号(同频次)':<20}{am:>11}%{md:>12}%")

    # 超额: chan vs bh, chan vs random
    diffs_bh = [c - b for c, b in zip(chan_rets, bh_rets)]
    diffs_rd = [c - r for c, r in zip(chan_rets, rand_rets)]
    am, md = _stat(diffs_bh)
    print(f"\nchan_lite - 买入持有:  均值 {am}%  中位 {md}%  <- 真正的超额(扣beta)")
    am, md = _stat(diffs_rd)
    print(f"chan_lite - 随机信号:  均值 {am}%  中位 {md}%  <- 超额(扣beta+止盈结构)")

    # 无交易比例
    print(f"\n无交易股票(0笔)占比: {n_no_trade}/{len(codes)} = {100.0*n_no_trade/len(codes):.0f}%")
    if n_trades_list:
        n_trades_list = [t for t in n_trades_list if t > 0]
        print(f"有交易股票的平均交易笔数: {sum(n_trades_list)/len(n_trades_list):.1f}")

    # 胜率分布: chan_lite 的盈利/亏损
    wins = sum(1 for r in chan_rets if r > 0)
    print(f"chan_lite 盈利股票占比: {wins}/{len(chan_rets)} = {100.0*wins/len(chan_rets):.0f}%")
    wins = sum(1 for r in bh_rets if r > 0)
    print(f"买入持有 盈利股票占比: {wins}/{len(bh_rets)} = {100.0*wins/len(bh_rets):.0f}%")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    asyncio.run(main(sample))
