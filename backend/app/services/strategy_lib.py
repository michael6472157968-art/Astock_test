"""策略库——15个经典量化策略的统一注册表。

所有策略签名统一：
    def strategy(daily_data: list[dict]) -> list[tuple[int, str]]

daily_data: [{"trade_date","open","high","low","close","volume","pct_chg"}, ...] ASC by date
返回: [(day_index, "buy"|"sell"), ...] 按 day_index 升序

零依赖：只依赖 factor_lib 的纯 Python 指标函数。
"""

from __future__ import annotations

from app.services.factor_lib import sma, ema, macd, rsi, kdj, atr, bollinger, momentum as _momentum


# ════════════════════════ 1-6: 已有策略（迁移自 backtest_engine） ════════════════════════

def s_ma_cross(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
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


def s_macd_cross(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
    m = macd(closes)
    dif, dea = m["dif"], m["dea"]
    sigs: list[tuple[int, str]] = []
    for i in range(1, len(closes)):
        if dif[i] is None or dea[i] is None or dif[i - 1] is None or dea[i - 1] is None:
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            sigs.append((i, "buy"))
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            sigs.append((i, "sell"))
    return sigs


def s_volume_break(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
    volumes = [r["volume"] for r in daily_data]
    ma20_vol = sma(volumes, 20)
    ma10 = sma(closes, 10)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(20, len(closes)):
        if None in (ma20_vol[i], ma10[i]):
            continue
        high20 = max(closes[i - 20:i])
        if not in_pos and volumes[i] > 1.5 * ma20_vol[i] and closes[i] > high20:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] < ma10[i]:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_boll_break(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
    n = 20
    ma = sma(closes, n)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(n, len(closes)):
        if ma[i] is None:
            continue
        window = closes[i - n + 1:i + 1]
        mean = sum(window) / n
        std = (sum((v - mean) ** 2 for v in window) / (n - 1)) ** 0.5
        lower = mean - 2 * std
        mid = mean
        if not in_pos and closes[i] < lower and closes[i - 1] >= lower:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] > mid:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_rsi_reversal(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
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


def s_momentum(daily_data: list[dict]) -> list[tuple[int, str]]:
    closes = [r["close"] for r in daily_data]
    volumes = [r["volume"] for r in daily_data]
    n = len(closes)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(20, n):
        roc5 = (closes[i] - closes[i - 5]) / closes[i - 5] if closes[i - 5] > 0 else 0
        roc20 = (closes[i] - closes[i - 20]) / closes[i - 20] if closes[i - 20] > 0 else 0
        vol_sum = sum(volumes[i - 19:i + 1])
        vol_ratio = volumes[i] / (vol_sum / 20) if vol_sum > 0 else 1
        if not in_pos and roc5 > 0.03 and roc20 > roc5 and vol_ratio > 1.2:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and roc5 < -0.02:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


# ════════════════════════ 7-9: 通道/均线系 ════════════════════════

def s_turtle(daily_data: list[dict]) -> list[tuple[int, str]]:
    """海龟交易法则：20日唐奇安通道突破买入 + 10日通道跌破卖出 + 2×ATR止损。"""
    closes = [r["close"] for r in daily_data]
    highs = [r["high"] for r in daily_data]
    lows = [r["low"] for r in daily_data]
    n = len(closes)
    a = atr(highs, lows, closes, 20)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    entry_price = 0.0
    stop_price = 0.0
    for i in range(20, n):
        if a[i] is None:
            continue
        high20 = max(highs[i - 20:i])
        low10 = min(lows[i - 10:i])
        if not in_pos:
            if closes[i] > high20:
                sigs.append((i, "buy"))
                in_pos = True
                entry_price = closes[i]
                stop_price = entry_price - 2 * a[i]
        else:
            if closes[i] < low10 or closes[i] < stop_price:
                sigs.append((i, "sell"))
                in_pos = False
    return sigs


def s_ma_bull_alignment(daily_data: list[dict]) -> list[tuple[int, str]]:
    """均线多头排列：MA5>10>20>60 四条均线对齐 + 收盘站稳MA5确认。"""
    closes = [r["close"] for r in daily_data]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(60, len(closes)):
        if None in (ma5[i], ma10[i], ma20[i], ma60[i]):
            continue
        aligned = ma5[i] > ma10[i] > ma20[i] > ma60[i]
        if not in_pos and aligned and closes[i] > ma5[i]:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and (not aligned or closes[i] < ma10[i]):
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_donchian_breakout(daily_data: list[dict]) -> list[tuple[int, str]]:
    """唐奇安55日通道突破：55日高点突破买入 + 20日低点跌破卖出（长周期趋势版）。"""
    closes = [r["close"] for r in daily_data]
    highs = [r["high"] for r in daily_data]
    lows = [r["low"] for r in daily_data]
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(55, len(closes)):
        high55 = max(highs[i - 55:i])
        low20 = min(lows[i - 20:i])
        if not in_pos and closes[i] > high55:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] < low20:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


# ════════════════════════ 10-12: 反转/反弹系 ════════════════════════

def s_boll_rebound(daily_data: list[dict]) -> list[tuple[int, str]]:
    """布林下轨反弹：收盘触及下轨 → 持仓等待回归中轨卖出。"""
    closes = [r["close"] for r in daily_data]
    n = 20
    ma = sma(closes, n)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(n, len(closes)):
        if ma[i] is None:
            continue
        window = closes[i - n + 1:i + 1]
        mean = sum(window) / n
        std = (sum((v - mean) ** 2 for v in window) / (n - 1)) ** 0.5
        lower = mean - 2 * std
        mid = mean
        near_lower = closes[i] <= lower * 1.01  # 1% tolerance
        if not in_pos and near_lower:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] >= mid:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_bias_reversal(daily_data: list[dict]) -> list[tuple[int, str]]:
    """乖离率回归：收盘偏离MA20超过-8%时买入 → 回复到MA20上方卖出。"""
    closes = [r["close"] for r in daily_data]
    ma20 = sma(closes, 20)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(20, len(closes)):
        if ma20[i] is None or ma20[i] == 0:
            continue
        bias = (closes[i] - ma20[i]) / ma20[i] * 100
        if not in_pos and bias < -8:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and bias >= 0:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_kdj_cross(daily_data: list[dict]) -> list[tuple[int, str]]:
    """KDJ金叉/死叉：K上穿D且在30以下为买入信号 + K下穿D且在70以上为卖出。"""
    closes = [r["close"] for r in daily_data]
    highs = [r["high"] for r in daily_data]
    lows = [r["low"] for r in daily_data]
    kdj_r = kdj(highs, lows, closes, 9)
    kv, dv = kdj_r["k"], kdj_r["d"]
    kdj_offset = 8  # kdj length = len(closes)-9+1, starts at day 8
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for ki in range(1, len(kv)):
        day_idx = ki + kdj_offset
        if kv[ki - 1] <= dv[ki - 1] and kv[ki] > dv[ki]:
            if not in_pos and kv[ki] < 30:
                sigs.append((day_idx, "buy"))
                in_pos = True
        elif kv[ki - 1] >= dv[ki - 1] and kv[ki] < dv[ki]:
            if in_pos and kv[ki] > 70:
                sigs.append((day_idx, "sell"))
                in_pos = False
    return sigs


# ════════════════════════ 13: 缩量突破 ════════════════════════

def s_volume_shrink_break(daily_data: list[dict]) -> list[tuple[int, str]]:
    """缩量突破：3日缩量后放量上涨突破前高 → 买入 + MA10止损卖出。"""
    closes = [r["close"] for r in daily_data]
    volumes = [r["volume"] for r in daily_data]
    ma20_vol = sma(volumes, 20)
    ma10 = sma(closes, 10)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    for i in range(25, len(closes)):
        if None in (ma20_vol[i], ma10[i]):
            continue
        v20 = ma20_vol[i]
        if v20 is None or v20 == 0:
            continue
        # 前3日持续缩量 (< 0.6x MA20)
        shrink = all(
            j >= 0 and volumes[j] < 0.6 * ma20_vol[j]
            for j in [i - 3, i - 2, i - 1]
            if j < len(volumes) and ma20_vol[j] is not None
        )
        high5 = max(closes[i - 5:i])
        if not in_pos and shrink and volumes[i] > 1.2 * v20 and closes[i] > high5:
            sigs.append((i, "buy"))
            in_pos = True
        elif in_pos and closes[i] < ma10[i]:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


# ════════════════════════ 14-15: K线形态 ════════════════════════

def s_hammer_pattern(daily_data: list[dict]) -> list[tuple[int, str]]:
    """锤子线形态：在下跌趋势中出现长下影线(>=2倍实体)+小实体+无上影线 → 反转买入。"""
    closes = [r["close"] for r in daily_data]
    opens = [r["open"] for r in daily_data]
    highs = [r["high"] for r in daily_data]
    lows = [r["low"] for r in daily_data]
    ma20 = sma(closes, 20)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    entry_low = 0.0
    for i in range(20, len(closes)):
        if ma20[i] is None:
            continue
        body = abs(closes[i] - opens[i])
        upper_shadow = highs[i] - max(closes[i], opens[i])
        lower_shadow = min(closes[i], opens[i]) - lows[i]
        is_hammer = (
            body > 0
            and lower_shadow >= 2 * body
            and upper_shadow <= body * 0.3
            and closes[i] < ma20[i]  # 下跌趋势中
        )
        if not in_pos and is_hammer:
            sigs.append((i, "buy"))
            in_pos = True
            entry_low = lows[i]
        elif in_pos and closes[i] < entry_low:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


def s_engulfing_pattern(daily_data: list[dict]) -> list[tuple[int, str]]:
    """看涨吞没形态：前日阴线 + 今日阳线完全吞没前日实体 + 放量确认 → 买入。"""
    closes = [r["close"] for r in daily_data]
    opens = [r["open"] for r in daily_data]
    highs = [r["high"] for r in daily_data]
    lows = [r["low"] for r in daily_data]
    volumes = [r["volume"] for r in daily_data]
    ma20 = sma(closes, 20)
    sigs: list[tuple[int, str]] = []
    in_pos = False
    entry_low = 0.0
    for i in range(21, len(closes)):
        if ma20[i] is None:
            continue
        prev_body = closes[i - 1] - opens[i - 1]
        curr_body = closes[i] - opens[i]
        is_engulfing = (
            prev_body < 0
            and curr_body > 0
            and opens[i] < closes[i - 1]
            and closes[i] > opens[i - 1]
            and volumes[i] > volumes[i - 1]
            and closes[i] < ma20[i]
        )
        if not in_pos and is_engulfing:
            sigs.append((i, "buy"))
            in_pos = True
            entry_low = min(lows[i], opens[i - 1])
        elif in_pos and closes[i] < entry_low:
            sigs.append((i, "sell"))
            in_pos = False
    return sigs


# ════════════════════════ 16: 缠论 lite ════════════════════════

def s_chan_lite(daily_data: list[dict]) -> list[tuple[int, str]]:
    """缠论 lite 版：包含处理→分型→笔→中枢→一买+三买+平仓。

    层1: K线包含处理（上升取高高，下降取低低）
    层2: 顶底分型 + 笔（相邻顶底，间隔≥5K线）
    层3: 中枢（连续3笔重叠区间，ZG=笔低点max, ZD=笔高点min, ZG≥ZD有效）
    层4: 一买=底分型+MACD背驰，三买=回调不破ZG，平仓=顶分型+破MA5或盈利5%回落2%
    """
    n = len(daily_data)
    closes = [float(r["close"]) for r in daily_data]
    highs = [float(r["high"]) for r in daily_data]
    lows = [float(r["low"]) for r in daily_data]

    # ── 层1: K线包含处理 ──
    idx_map = list(range(n))
    h = list(highs)
    l_vals = list(lows)
    direction = 1
    j = 1
    while j < len(h):
        prev_contains = h[j - 1] >= h[j] and l_vals[j - 1] <= l_vals[j]
        curr_contains = h[j] >= h[j - 1] and l_vals[j] <= l_vals[j - 1]
        if prev_contains or curr_contains:
            if direction == 1:
                h[j - 1] = max(h[j - 1], h[j])
                l_vals[j - 1] = max(l_vals[j - 1], l_vals[j])
            else:
                h[j - 1] = min(h[j - 1], h[j])
                l_vals[j - 1] = min(l_vals[j - 1], l_vals[j])
            h.pop(j)
            l_vals.pop(j)
            idx_map.pop(j)
        else:
            if j >= 1 and h[j] > h[j - 1] and l_vals[j] > l_vals[j - 1]:
                direction = 1
            elif j >= 1 and h[j] < h[j - 1] and l_vals[j] < l_vals[j - 1]:
                direction = -1
            j += 1

    # ── 层2: 分型识别 ──
    m = len(h)
    tops: list[tuple[int, float]] = []
    bottoms: list[tuple[int, float]] = []
    for i in range(1, m - 1):
        if h[i] > h[i - 1] and h[i] > h[i + 1] and l_vals[i] > l_vals[i - 1] and l_vals[i] > l_vals[i + 1]:
            tops.append((idx_map[i], h[i]))
        if l_vals[i] < l_vals[i - 1] and l_vals[i] < l_vals[i + 1] and h[i] < h[i - 1] and h[i] < h[i + 1]:
            bottoms.append((idx_map[i], l_vals[i]))

    tps = [(idx, val, "top") for idx, val in tops] + [(idx, val, "bottom") for idx, val in bottoms]
    tps.sort(key=lambda x: x[0])

    # ── 层2b: 笔的构建 ──
    strokes: list[tuple] = []
    pending = None
    for pt in tps:
        if pending is None:
            pending = pt
            continue
        if pt[2] == pending[2]:
            if pending[2] == "top" and pt[1] > pending[1]:
                pending = pt
            elif pending[2] == "bottom" and pt[1] < pending[1]:
                pending = pt
        else:
            if pt[0] - pending[0] >= 5:
                strokes.append((pending, pt))
                pending = pt

    if len(strokes) < 3:
        return []

    # ── 层3: 中枢识别 ──
    pivots: list[tuple[float, float]] = []
    for si in range(len(strokes) - 2):
        s0, s1, s2 = strokes[si], strokes[si + 1], strokes[si + 2]
        s_highs = [max(s[0][1], s[1][1]) for s in (s0, s1, s2)]
        s_lows = [min(s[0][1], s[1][1]) for s in (s0, s1, s2)]
        zg = min(s_highs)
        zd = max(s_lows)
        if zg > zd:
            pivots.append((zg, zd))

    zg = pivots[-1][0] if pivots else 0.0

    # ── 层4: 买卖点 ──
    macd_data = macd(closes)
    macd_bar = macd_data["bar"]
    ma5 = sma(closes, 5)

    def _bar_area(a: int, b: int) -> float:
        total = 0.0
        for k in range(a, min(b + 1, len(macd_bar))):
            if macd_bar[k] is not None:
                total += abs(macd_bar[k])
        return total

    # Collect down-strokes for divergence
    down_strokes = [(s[0][0], s[1][0]) for s in strokes if s[0][2] == "top" and s[1][2] == "bottom"]

    sigs: list[tuple[int, str]] = []
    in_pos = False
    entry_price = 0.0
    peak_price = 0.0

    for i in range(n):
        # ── 平仓 ──
        if in_pos:
            peak_price = max(peak_price, closes[i])
            profit_pct = (closes[i] - entry_price) / entry_price * 100

            exit_sig = False
            # 顶分型 + 破MA5
            if any(t[0] == i for t in tops) and ma5[i] is not None and closes[i] < ma5[i]:
                exit_sig = True
            # 盈利>5% 回落>2%
            elif profit_pct > 5 and (peak_price - closes[i]) / peak_price >= 0.02:
                exit_sig = True

            if exit_sig:
                sigs.append((i, "sell"))
                in_pos = False
                continue

        # ── 买入 ──
        if not in_pos:
            bot_match = [b for b in bottoms if b[0] == i]
            if not bot_match:
                continue
            bot_price = bot_match[0][1]

            # 一买: 底分型 + MACD柱面积背驰
            ds_idx = next((di for di, (_, end) in enumerate(down_strokes) if end == i), -1)
            if ds_idx >= 1:
                cur_a = _bar_area(down_strokes[ds_idx][0], down_strokes[ds_idx][1])
                prev_a = _bar_area(down_strokes[ds_idx - 1][0], down_strokes[ds_idx - 1][1])
                if prev_a > 0 and cur_a < prev_a * 0.85:
                    sigs.append((i, "buy"))
                    in_pos = True
                    entry_price = closes[i]
                    peak_price = closes[i]
                    continue

            # 三买: 回调不破ZG
            if zg > 0 and bot_price > zg:
                is_pullback = any(s[1][0] == i and s[0][2] == "top" for s in strokes)
                if is_pullback:
                    sigs.append((i, "buy"))
                    in_pos = True
                    entry_price = closes[i]
                    peak_price = closes[i]

    return sigs


# ════════════════════════ 注册表 ════════════════════════

STRATEGIES: dict[str, callable] = {
    "ma_cross": s_ma_cross,
    "macd_cross": s_macd_cross,
    "volume_break": s_volume_break,
    "boll_break": s_boll_break,
    "rsi_reversal": s_rsi_reversal,
    "momentum": s_momentum,
    "turtle": s_turtle,
    "ma_bull_alignment": s_ma_bull_alignment,
    "boll_rebound": s_boll_rebound,
    "bias_reversal": s_bias_reversal,
    "volume_shrink_break": s_volume_shrink_break,
    "kdj_cross": s_kdj_cross,
    "donchian_breakout": s_donchian_breakout,
    "hammer_pattern": s_hammer_pattern,
    "engulfing_pattern": s_engulfing_pattern,
    "chan_lite": s_chan_lite,
}

STRATEGY_META: dict[str, dict] = {
    "ma_cross":               {"name": "均线交叉",       "cat": "趋势", "desc": "MA5上穿MA20金叉买入，下穿死叉卖出", "params": {"ma_fast": 5, "ma_slow": 20}},
    "macd_cross":             {"name": "MACD金叉死叉",    "cat": "趋势", "desc": "DIF上穿DEA买入，下穿卖出", "params": {"dif_n": 12, "dea_n": 26, "signal_n": 9}},
    "volume_break":           {"name": "放量突破",       "cat": "突破", "desc": "放量(>1.5倍均量)突破20日高点买入，跌破MA10卖出", "params": {"vol_mult": 1.5, "window": 20}},
    "boll_break":             {"name": "布林带突破",      "cat": "反转", "desc": "收盘跌破布林下轨(2σ)买入，回归中轨卖出", "params": {"window": 20, "k": 2.0}},
    "rsi_reversal":           {"name": "RSI反转",       "cat": "反转", "desc": "14日RSI<30超卖买入，>70超买卖出", "params": {"period": 14, "oversold": 30, "overbought": 70}},
    "momentum":               {"name": "双动量",         "cat": "动量", "desc": "5日动量>3%且20日趋势确认+放量买入", "params": {"roc5_min": 0.03, "vol_min": 1.2}},
    "turtle":                 {"name": "海龟交易法",      "cat": "趋势", "desc": "20日高点突破买入，10日低点跌破/2xATR止损卖出", "params": {"entry_days": 20, "exit_days": 10, "atr_mult": 2}},
    "ma_bull_alignment":      {"name": "均线多头排列",     "cat": "趋势", "desc": "MA5>10>20>60四线对齐+收盘站上MA5买入", "params": {"ma_list": [5, 10, 20, 60]}},
    "boll_rebound":           {"name": "布林下轨反弹",     "cat": "反转", "desc": "收盘触及布林下轨即买入，等回归中轨卖出", "params": {"window": 20, "k": 2.0}},
    "bias_reversal":          {"name": "乖离率回归",      "cat": "反转", "desc": "收盘偏离MA20超-8%买入，回升至MA20卖出", "params": {"window": 20, "threshold": -8}},
    "volume_shrink_break":    {"name": "缩量突破",       "cat": "突破", "desc": "3日缩量蓄力后放量突破5日高点买入", "params": {"shrink_days": 3, "vol_shrink": 0.6}},
    "kdj_cross":              {"name": "KDJ金叉死叉",    "cat": "反转", "desc": "K上穿D且<30超卖区买入，下穿且>70卖出", "params": {"n": 9, "oversold": 30, "overbought": 70}},
    "donchian_breakout":      {"name": "唐奇安通道",      "cat": "趋势", "desc": "55日高点突破买入，20日低点跌破卖出", "params": {"entry_days": 55, "exit_days": 20}},
    "hammer_pattern":         {"name": "锤子线形态",      "cat": "形态", "desc": "下跌趋势中出现长下影锤子线→反转买入", "params": {"shadow_mult": 2.0}},
    "engulfing_pattern":      {"name": "看涨吞没形态",     "cat": "形态", "desc": "前日阴线+今日阳线完全吞没+放量→反转买入", "params": {"volume_confirm": True}},
    "chan_lite":              {"name": "缠论lite",        "cat": "缠论", "desc": "包含处理→分型→笔→中枢→一买(MACD背驰)+三买(回调不破ZG)，顶分型+破MA5平仓", "params": {"stroke_min": 5, "divg_threshold": 0.85}},
}
