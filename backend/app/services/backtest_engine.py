"""策略回测引擎——统一回测执行器。

15种策略通过 strategy_lib.STRATEGIES 注册表接入。
所有策略统一接受 daily_data 作为输入 → 返回信号列表。

daily_data: [{"trade_date","open","high","low","close","volume","pct_chg"}, ...] ASC
返回: {equity_curve, trades, metrics, buy_signals, sell_signals, drawdown_curve, monthly_returns}
"""

from __future__ import annotations

import hashlib
import json

from app.services.strategy_lib import STRATEGIES, STRATEGY_META


def _reason(strategy: str, action: str) -> str:
    """可覆盖的信号注释。默认使用策略名+动作。"""
    names: dict[str, dict[str, str]] = {
        "ma_cross": {"buy": "金叉买入", "sell": "死叉卖出"},
        "macd_cross": {"buy": "MACD金叉", "sell": "MACD死叉"},
        "volume_break": {"buy": "放量突破买入", "sell": "跌破MA10卖出"},
        "boll_break": {"buy": "布林下轨突破", "sell": "回归中轨"},
        "rsi_reversal": {"buy": "RSI超卖买入", "sell": "RSI超买卖出"},
        "momentum": {"buy": "动量突破", "sell": "动量衰减"},
        "turtle": {"buy": "海龟突破买入", "sell": "海龟止损"},
        "ma_bull_alignment": {"buy": "多头排列买入", "sell": "排列破坏"},
        "boll_rebound": {"buy": "触及下轨反弹", "sell": "回归中轨卖出"},
        "bias_reversal": {"buy": "乖离过大买入", "sell": "回归均线"},
        "volume_shrink_break": {"buy": "缩量突破买入", "sell": "跌破MA10"},
        "kdj_cross": {"buy": "KDJ金叉买入", "sell": "KDJ死叉卖出"},
        "donchian_breakout": {"buy": "唐奇安突破", "sell": "通道跌破"},
        "hammer_pattern": {"buy": "锤子线买入", "sell": "止损卖出"},
        "engulfing_pattern": {"buy": "吞没形态买入", "sell": "止损卖出"},
        "chan_lite":          {"buy": "缠论买入", "sell": "缠论平仓"},
    }
    return names.get(strategy, {}).get(action, action)


def _hash_params(params: dict) -> str:
    return hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]


# ── 交易成本 ──
_STAMP_TAX = 0.0005
_COMMISSION = 0.00025
_MIN_COMM = 5.0


def _trade_cost(amount: float, action: str) -> float:
    comm = max(amount * _COMMISSION, _MIN_COMM)
    if action == "sell":
        comm += amount * _STAMP_TAX
    return comm


def _is_limit_up(pct_chg: float | None) -> bool:
    if pct_chg is None:
        return False
    return float(pct_chg) >= 9.8


def cache_key(ts_code: str, strategy: str, params: dict | None = None) -> str:
    p = _hash_params(params or {})
    return f"bt:{ts_code}:{strategy}:{p}"


def run(daily_data: list[dict], strategy: str = "ma_cross",
        initial_cash: float = 100000) -> dict:
    """执行回测。

    B1: 交易成本（印花税+佣金+最低5元）
    B2: 信号日收盘触发 → 次日开盘价成交（消除未来函数）
    B3: 买入日遇涨停跳过
    """
    if strategy not in STRATEGIES:
        existing = ", ".join(STRATEGIES.keys())
        return {"error": f"未知策略 '{strategy}'，可用: {existing}"}

    signal_fn = STRATEGIES[strategy]
    signals = signal_fn(daily_data)

    dates = [r["trade_date"] for r in daily_data]
    opens = [float(r.get("open", 0) or 0) for r in daily_data]
    closes = [float(r["close"]) for r in daily_data]
    pct_chgs = [r.get("pct_chg") for r in daily_data]

    cash = float(initial_cash)
    shares = 0
    trades: list[dict] = []
    equity: list[list] = []
    buy_sigs: list[list] = []
    sell_sigs: list[list] = []

    pending_action: str | None = None
    pending_reason: str = ""

    sp = 0
    n = len(dates)
    for i in range(n):
        # Step 1: 执行 pending 交易（次日开盘）
        if pending_action is not None and opens[i] > 0:
            px = opens[i]
            if pending_action == "buy":
                if not _is_limit_up(pct_chgs[i]):
                    if cash >= 100:
                        buy_shares = int(cash * 0.98 / px / 100) * 100
                        if buy_shares <= 0:
                            buy_shares = 10
                        amount = buy_shares * px
                        fee = _trade_cost(amount, "buy")
                        cash -= amount + fee
                        shares += buy_shares
                        trades.append({
                            "date": dates[i], "action": "buy", "price": round(px, 2),
                            "shares": buy_shares, "amount": round(amount, 2),
                            "fee": round(fee, 2), "reason": pending_reason,
                        })
                        buy_sigs.append([dates[i], round(px, 2)])
            elif pending_action == "sell" and shares > 0:
                amount = shares * px
                fee = _trade_cost(amount, "sell")
                cash += amount - fee
                trades.append({
                    "date": dates[i], "action": "sell", "price": round(px, 2),
                    "shares": shares, "amount": round(amount, 2),
                    "fee": round(fee, 2), "reason": pending_reason,
                })
                sell_sigs.append([dates[i], round(px, 2)])
                shares = 0
            pending_action = None

        # Step 2: 从今日收盘信号生成 pending（次日执行）
        if i < n - 1:
            while sp < len(signals) and signals[sp][0] == i:
                _, action = signals[sp]
                if action == "buy" and shares == 0 and pending_action is None:
                    pending_action = "buy"
                    pending_reason = _reason(strategy, "buy")
                elif action == "sell" and shares > 0:
                    pending_action = "sell"
                    pending_reason = _reason(strategy, "sell")
                sp += 1

        # Step 3: 按当日收盘价记录权益
        equity.append([dates[i], round(cash + shares * closes[i], 2)])

    # 期末平仓
    if shares > 0:
        last_px = closes[-1]
        amount = shares * last_px
        fee = _trade_cost(amount, "sell")
        cash += amount - fee
        trades.append({
            "date": dates[-1], "action": "sell", "price": round(last_px, 2),
            "shares": shares, "amount": round(amount, 2),
            "fee": round(fee, 2), "reason": "期末平仓",
        })
        sell_sigs.append([dates[-1], round(last_px, 2)])
        shares = 0
        equity[-1][1] = round(cash, 2)

    metrics = _metrics(equity, trades, initial_cash)

    # 买入持有基准（期初全仓，同一初始资金，逐日市值）
    bh_cash = float(initial_cash)
    bh_shares = 0
    buy_hold_equity = []
    for i in range(n):
        if i == 0 and opens[i] > 0:
            px = opens[i]
            bs = int(bh_cash * 0.98 / px / 100) * 100
            if bs <= 0:
                bs = 10
            amount = bs * px
            bh_cash -= amount + _trade_cost(amount, "buy")
            bh_shares += bs
        buy_hold_equity.append([dates[i], round(bh_cash + bh_shares * closes[i], 2)])
    if bh_shares > 0:
        bh_cash += bh_shares * closes[-1] - _trade_cost(bh_shares * closes[-1], "sell")
        buy_hold_equity[-1][1] = round(bh_cash, 2)
    bh_total_ret = round((bh_cash - initial_cash) / initial_cash * 100, 2)
    metrics["buy_hold_return"] = bh_total_ret
    metrics["excess_return"] = round(metrics["total_return"] - bh_total_ret, 2)

    return {
        "equity_curve": equity,
        "buy_hold_curve": buy_hold_equity,
        "trades": trades,
        "metrics": metrics,
        "buy_signals": buy_sigs,
        "sell_signals": sell_sigs,
        "drawdown_curve": _drawdown(equity),
        "monthly_returns": _monthly_returns(equity),
    }


# ── 统计指标 ──

def _drawdown(equity: list[list]) -> list[list]:
    dd = []
    peak = 0.0
    for dt, v in equity:
        peak = max(peak, v)
        dd_pct = round((peak - v) / peak * 100, 2) if peak > 0 else 0
        dd.append([dt, dd_pct])
    return dd


def _monthly_returns(equity: list[list]) -> list[list]:
    by_month: dict[str, list[float]] = {}
    for dt, v in equity:
        ym = dt[:6]
        by_month.setdefault(ym, []).append(v)
    months = []
    for ym, vals in sorted(by_month.items()):
        first, last = vals[0], vals[-1]
        ret = round((last - first) / first * 100, 2) if first > 0 else 0
        months.append([ym, ret, f"{ret:+.2f}%"])
    return months


def _metrics(equity: list[list], trades: list[dict], initial: float) -> dict:
    final_eq = equity[-1][1]
    total_ret = round((final_eq - initial) / initial * 100, 2)
    trading_days = len(equity)
    years = trading_days / 252
    ann_ret = round((final_eq / initial) ** (1 / years) * 100 - 100, 2) if years > 0 else 0

    winds, losses = [], []
    used: set[int] = set()
    for i, t in enumerate(trades):
        if t["action"] != "buy":
            continue
        sell_idx = -1
        for j in range(i + 1, len(trades)):
            if trades[j]["action"] == "sell" and j not in used:
                sell_idx = j
                break
        if sell_idx == -1:
            continue
        used.add(sell_idx)
        pnl = trades[sell_idx]["amount"] - t["amount"]
        pnl -= t.get("fee", 0) + trades[sell_idx].get("fee", 0)
        (winds if pnl > 0 else losses).append(abs(pnl))

    n = len(winds) + len(losses)
    win_rate = round(len(winds) / n * 100, 1) if n else 0
    avg_win = round(sum(winds) / len(winds), 2) if winds else 0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0
    plr = round(avg_win / avg_loss, 2) if avg_loss else 0

    peak, max_dd = equity[0][1], 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    eq_vals = [v for _, v in equity]
    d_ret = [
        (eq_vals[i] - eq_vals[i - 1]) / eq_vals[i - 1]
        for i in range(1, len(eq_vals)) if eq_vals[i - 1]
    ]
    if d_ret:
        m = sum(d_ret) / len(d_ret)
        var = sum((r - m) ** 2 for r in d_ret) / len(d_ret)
        std = var ** 0.5
        sr = round(m / std * (252 ** 0.5), 2) if std else 0
    else:
        sr = 0

    return {
        "total_return": total_ret,
        "annual_return": ann_ret,
        "win_rate": win_rate,
        "profit_loss_ratio": plr,
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe_ratio": sr,
        "total_trades": n,
        "winning_trades": len(winds),
        "losing_trades": len(losses),
    }
