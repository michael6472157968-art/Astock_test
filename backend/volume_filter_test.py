"""量比过滤器能否提升多头策略的超额收益？

量比>2 = 见顶日（已证 alpha，但方向是做空，A股多头策略无法直接做空）。
这里验证量比在多头策略里唯一合理的用法——「离场过滤器」：
  1. 买入信号日若量比>=2 → 跳过买入（避免追高见顶）
  2. 持仓中若量比>=2 → 强制卖出（见顶离场）

对比口径：超额收益（策略收益 - buy&hold 收益），不是绝对收益。
复用 backtest_engine 的成交逻辑+成本，只注入量比过滤。

用法: python volume_filter_test.py [sample_size]
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, 'backend')

from app.services.calibration import _sample_stocks, _load_daily_data_fast
from app.services.backtest_engine import _trade_cost, _is_limit_up
from app.services.strategy_lib import STRATEGIES

INIT_CASH = 100000.0


def _vol_ratios(daily: list[dict]) -> list[float]:
    """逐日量比 = volume[i] / mean(volume[i-20:i])，前20日无值记1.0。"""
    vols = [float(r.get("volume", 0) or 0) for r in daily]
    out = []
    for i in range(len(vols)):
        if i < 20:
            out.append(1.0)
            continue
        w = vols[i - 20:i]
        avg = sum(w) / len(w)
        out.append(vols[i] / avg if avg > 0 else 1.0)
    return out


def _simulate(daily: list[dict], signals: list[tuple[int, str]], vol_filter: bool) -> dict:
    """简化回测循环（对齐 backtest_engine），vol_filter=True 时注入量比离场过滤器。"""
    n = len(daily)
    dates = [r["trade_date"] for r in daily]
    opens = [float(r.get("open", 0) or 0) for r in daily]
    closes = [float(r["close"]) for r in daily]
    pct_chgs = [r.get("pct_chg") for r in daily]
    vr = _vol_ratios(daily)

    cash = INIT_CASH
    shares = 0
    pending: str | None = None
    sp = 0
    n_trades = 0

    for i in range(n):
        # Step 1: 执行 pending（次日开盘）
        if pending is not None and opens[i] > 0:
            px = opens[i]
            if pending == "buy":
                if not _is_limit_up(pct_chgs[i]):
                    if cash >= 100:
                        bs = int(cash * 0.98 / px / 100) * 100
                        if bs <= 0:
                            bs = 10
                        amount = bs * px
                        cash -= amount + _trade_cost(amount, "buy")
                        shares += bs
                        n_trades += 1
            elif pending == "sell" and shares > 0:
                amount = shares * px
                cash += amount - _trade_cost(amount, "sell")
                shares = 0
                n_trades += 1
            pending = None

        # Step 2: 量比离场过滤器（持仓中遇放量见顶 → 强制卖出）
        if vol_filter and shares > 0 and i < n - 1 and vr[i] >= 2.0 and pending is None:
            pending = "sell"

        # Step 3: 从今日收盘信号生成 pending
        if i < n - 1:
            while sp < len(signals) and signals[sp][0] == i:
                _, action = signals[sp]
                # 量比过滤器：放量日禁止买入
                if action == "buy" and vol_filter and vr[i] >= 2.0:
                    sp += 1
                    continue
                if action == "buy" and shares == 0 and pending is None:
                    pending = "buy"
                elif action == "sell" and shares > 0 and pending is None:
                    pending = "sell"
                sp += 1

    # 期末平仓
    if shares > 0:
        amount = shares * closes[-1]
        cash += amount - _trade_cost(amount, "sell")
        shares = 0

    return {"final_cash": cash, "n_trades": n_trades}


def _buy_hold_return(daily: list[dict]) -> float:
    opens = [float(r.get("open", 0) or 0) for r in daily]
    closes = [float(r["close"]) for r in daily]
    cash = INIT_CASH
    shares = 0
    if opens[0] > 0:
        bs = int(cash * 0.98 / opens[0] / 100) * 100
        if bs <= 0:
            bs = 10
        amount = bs * opens[0]
        cash -= amount + _trade_cost(amount, "buy")
        shares += bs
    cash += shares * closes[-1] - _trade_cost(shares * closes[-1], "sell")
    return (cash - INIT_CASH) / INIT_CASH * 100


async def main(sample: int):
    codes = await _sample_stocks(sample)
    strategies = ["ma_cross", "turtle", "donchian_breakout", "chan_lite"]

    # strat -> (原版超额列表, 过滤版超额列表)
    results: dict[str, list] = {s: [[], []] for s in strategies}

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < 120:
            continue
        bh = _buy_hold_return(daily)

        for strat in strategies:
            try:
                sigs = STRATEGIES[strat](daily)
            except Exception:
                continue
            # 原版
            r0 = _simulate(daily, sigs, vol_filter=False)
            exc0 = (r0["final_cash"] - INIT_CASH) / INIT_CASH * 100 - bh
            # 过滤版
            r1 = _simulate(daily, sigs, vol_filter=True)
            exc1 = (r1["final_cash"] - INIT_CASH) / INIT_CASH * 100 - bh
            results[strat][0].append(exc0)
            results[strat][1].append(exc1)

    def _stat(xs):
        if not xs:
            return (0.0, 0.0)
        xs = sorted(xs)
        return (round(sum(xs) / len(xs), 2), round(xs[len(xs) // 2], 2))

    print(f"\n=== 量比离场过滤器对多头策略超额收益的影响 (样本{len(codes)}股, 2年) ===")
    print(f"超额收益 = 策略收益 - buy&hold收益（正数=跑赢基准）\n")
    print(f"{'策略':<18}{'原版均值':>10}{'过滤均值':>10}{'原版中位':>10}{'过滤中位':>10}{'改善':>8}")
    for strat in strategies:
        m0, md0 = _stat(results[strat][0])
        m1, md1 = _stat(results[strat][1])
        imp = m1 - m0
        print(f"{strat:<18}{m0:>9}%{m1:>9}%{md0:>9}%{md1:>9}%{imp:>+7.2f}pp")

    # 过滤版 vs 原版 的股票级差异分布
    print("\n--- 股票级：过滤版减原版的超额改善 ---")
    for strat in strategies:
        diffs = [a - b for a, b in zip(results[strat][1], results[strat][0])]
        if not diffs:
            print(f"{strat:<18} 无有效样本")
            continue
        win = sum(1 for d in diffs if d > 0)
        m, md = _stat(diffs)
        print(f"{strat:<18} 改善股票 {win}/{len(diffs)} ({100.0*win/len(diffs):.0f}%)  均值 {m:+.2f}pp  中位 {md:+.2f}pp")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    asyncio.run(main(sample))
