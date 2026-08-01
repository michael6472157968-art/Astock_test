"""策略回测引擎。

三种策略的逐日模拟交易：
- ma_cross: 5/20日简单移动平均交叉
- volume_break: 放量(>1.5x20日均量)突破20日收盘高点 + MA10止损
- rsi_reversal: 14日RSI超卖(<30)买入 / 超买(>70)卖出
"""

from __future__ import annotations


def sma(series: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(series)
    for i in range(n - 1, len(series)):
        out[i] = sum(series[i - n + 1 : i + 1]) / n
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0)
        losses.append(-d if d < 0 else 0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        rsi_val = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        result[i + 1] = round(rsi_val, 2)
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return result


def _signals_ma_cross(closes: list[float], _volumes: list[float]) -> list[tuple[int, str]]:
    ma5 = sma(closes, 5)
    ma20 = sma(closes, 20)
    sigs: list[tuple[int, str]] = []
    for i in range(1, len(closes)):
        if None in (ma5[i], ma20[i], ma5[i - 1], ma20[i - 1]):
            continue
        if ma5[i - 1] <= ma20[i - 1] and ma5[i] > ma20[i]:
            sigs.append((i, "buy"))
        elif ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]:
            sigs.append((i, "sell"))
    return sigs


def _signals_volume_break(closes: list[float], volumes: list[float]) -> list[tuple[int, str]]:
    ma20_vol = sma(volumes, 20)
    ma10 = sma(closes, 10)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(20, len(closes)):
        if None in (ma20_vol[i], ma10[i]):
            continue
        high20 = max(closes[i - 20 : i])  # 前20日最高价（不含当日）
        if not in_pos and volumes[i] > 1.5 * ma20_vol[i] and closes[i] > high20:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] < ma10[i]:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def _signals_rsi_reversal(closes: list[float], _volumes: list[float]) -> list[tuple[int, str]]:
    rsi14 = rsi(closes, 14)
    sigs: list[tuple[int, str]] = []
    for i in range(1, len(closes)):
        if None in (rsi14[i], rsi14[i - 1]):
            continue
        if rsi14[i - 1] >= 30 and rsi14[i] < 30:
            sigs.append((i, "buy"))
        elif rsi14[i - 1] <= 70 and rsi14[i] > 70:
            sigs.append((i, "sell"))
    return sigs


_SIGNAL_FNS = {
    "ma_cross": _signals_ma_cross,
    "volume_break": _signals_volume_break,
    "rsi_reversal": _signals_rsi_reversal,
}


def run(daily_data: list[dict], strategy: str = "ma_cross", initial_cash: float = 100000) -> dict:
    """执行回测，返回 {equity_curve, trades, metrics, buy_signals, sell_signals}。

    daily_data: [{"trade_date": "20240102", "close": 10.5, "volume": 123456}, ...] ASC by date
    """
    dates = [r["trade_date"] for r in daily_data]
    closes = [float(r["close"]) for r in daily_data]
    volumes = [float(r.get("volume", 0) or 0) for r in daily_data]

    signals = _SIGNAL_FNS[strategy](closes, volumes)

    cash = float(initial_cash)
    shares = 0
    trades: list[dict] = []
    equity: list[list] = []
    buy_sigs: list[list] = []
    sell_sigs: list[list] = []

    def _execute(i: int, action: str):
        nonlocal cash, shares
        px = closes[i]
        dt = dates[i]
        if action == "buy" and cash >= 100:
            buy_shares = int(cash * 0.98 / px / 100) * 100
            if buy_shares <= 0:
                buy_shares = 10  # 高价股买不起1手时至少买10股模拟
            cost = round(buy_shares * px, 2)
            cash -= cost
            shares += buy_shares
            trades.append({"date": dt, "action": "buy", "price": round(px, 2),
                           "shares": buy_shares, "amount": cost,
                           "reason": _reason(strategy, "buy")})
            buy_sigs.append([dt, round(px, 2)])
        elif action == "sell" and shares > 0:
            proceeds = round(shares * px, 2)
            cash += proceeds
            trades.append({"date": dt, "action": "sell", "price": round(px, 2),
                           "shares": shares, "amount": proceeds,
                           "reason": _reason(strategy, "sell")})
            sell_sigs.append([dt, round(px, 2)])
            shares = 0

    sp = 0
    for i in range(len(dates)):
        while sp < len(signals) and signals[sp][0] == i:
            _execute(i, signals[sp][1])
            sp += 1
        equity.append([dates[i], round(cash + shares * closes[i], 2)])

    # 期末强平
    if shares > 0:
        last_px = closes[-1]
        cash += round(shares * last_px, 2)
        trades.append({"date": dates[-1], "action": "sell", "price": round(last_px, 2),
                       "shares": shares, "amount": round(shares * last_px, 2),
                       "reason": "期末平仓"})
        sell_sigs.append([dates[-1], round(last_px, 2)])
        shares = 0
        equity[-1][1] = round(cash, 2)

    return {
        "equity_curve": equity,
        "trades": trades,
        "metrics": _metrics(equity, trades, initial_cash),
        "buy_signals": buy_sigs,
        "sell_signals": sell_sigs,
    }


def _reason(strategy: str, action: str) -> str:
    m = {
        "ma_cross": {"buy": "金叉买入", "sell": "死叉卖出"},
        "volume_break": {"buy": "放量突破买入", "sell": "跌破MA10卖出"},
        "rsi_reversal": {"buy": "RSI超卖买入", "sell": "RSI超买卖出"},
    }
    return m.get(strategy, {}).get(action, action)


def _metrics(equity: list[list], trades: list[dict], initial: float) -> dict:
    final_eq = equity[-1][1]
    total_ret = round((final_eq - initial) / initial * 100, 2)
    trading_days = len(equity)
    years = trading_days / 252
    ann_ret = round((final_eq / initial) ** (1 / years) * 100 - 100, 2) if years > 0 else 0

    # 配对交易统计（按截面配对：每笔buy匹配下一笔sell）
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
        (winds if pnl > 0 else losses).append(abs(pnl))
    n = len(winds) + len(losses)
    win_rate = round(len(winds) / n * 100, 1) if n else 0
    avg_win = round(sum(winds) / len(winds), 2) if winds else 0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0
    plr = round(avg_win / avg_loss, 2) if avg_loss else 0

    # 最大回撤
    peak, max_dd = equity[0][1], 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    # 日收益率 std
    eq_vals = [v for _, v in equity]
    d_ret = [(eq_vals[i] - eq_vals[i - 1]) / eq_vals[i - 1] for i in range(1, len(eq_vals)) if eq_vals[i - 1]]
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
