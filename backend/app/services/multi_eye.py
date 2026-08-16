"""五眼共识引擎 — 蜡烛·指标·缠论·波浪·江恩，统一签名，投票决策。

每只眼返回 EyeVerdict(trend/position/signal + confidence)。
共识器取多数票给出最终 trend/position/signal 结论。
"""

from __future__ import annotations

import json
import math
import os

from dataclasses import dataclass, field

from app.services.factor_lib import sma, ema, macd, rsi, kdj, bollinger, atr


@dataclass
class EyeVerdict:
    eye: str           # candle | indicator | chan | wave | gann
    lens: str          # 博弈·情绪 | 数据·异常 | 级别·层次 | 形态·结构 | 时间·节奏
    trend: str         # up | down | neutral
    trend_detail: str
    position: str      # key_level | approaching | mid_range
    position_detail: str
    signal: str        # buy | sell | caution | none
    signal_detail: str
    confidence: int    # 1-5
    horizon: str = ""  # short(3-7d) | mid(7-15d) | long(15-30d) — 信号兑现的时间尺度


@dataclass
class ConsensusResult:
    eyes: dict[str, EyeVerdict]
    trend: dict
    position: dict
    signal: dict
    summary: str
    plain_summary: str
    retreat_alert: dict = field(default_factory=dict)


# ════════════════════════ 辅助函数 ════════════════════════

def _safe(a: list, i: int, default=0.0):
    if i < 0 or i >= len(a):
        return default
    v = a[i]
    return default if v is None else v


def _slope(s: list[float], lookback: int = 5) -> float:
    """最近 lookback 天的线性回归斜率。"""
    n = len(s)
    if n < lookback:
        return 0.0
    ys = s[-lookback:]
    xs = list(range(lookback))
    m_x = (lookback - 1) / 2.0
    m_y = sum(ys) / lookback
    num = sum((x - m_x) * (y - m_y) for x, y in zip(xs, ys))
    den = sum((x - m_x) ** 2 for x in xs)
    return num / den if den != 0 else 0.0


def _swing_points(highs: list[float], lows: list[float], lookback: int = 5):
    """简单波峰/波谷检测 (不含复杂波浪计数)。"""
    n = len(highs)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(lookback, n - lookback):
        is_high = all(highs[i] >= highs[i - k] for k in range(1, lookback + 1)) and \
                  all(highs[i] >= highs[i + k] for k in range(1, lookback + 1))
        is_low = all(lows[i] <= lows[i - k] for k in range(1, lookback + 1)) and \
                 all(lows[i] <= lows[i + k] for k in range(1, lookback + 1))
        if is_high:
            swing_highs.append((i, highs[i]))
        if is_low:
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


# ════════════════════════ 蜡烛形态库（TA-Lib学术定义） ════════════════════════
#
# 阈值体系源自 TA-Lib C 源码 CandleSetting 默认值：
#   avg_body = SMA(real_body, 8)  # 8日平均实体
#   BodyDoji <= avg_body * 0.08   # 十字星/蜻蜓/墓碑
#   BodyShort <= avg_body * 0.50  # 短实体（纺锤线）
#   BodyLong >= avg_body * 1.50   # 长实体（光头光脚）
#   影线阈值同理：ShadowVeryShort/Short/Long
#   穿透确认：刺透线收盘需 > 前阴线中点（penetration >= 50%）
#   跳空确认：gap >= avg_body * 0.05（最少 5% 均体）

# ── 基础度量 ──

def _body(opens: list[float], closes: list[float], i: int) -> float:
    """实体大小（带符号：正=阳线，负=阴线）。"""
    return closes[i] - opens[i]


def _body_abs(opens: list[float], closes: list[float], i: int) -> float:
    return abs(closes[i] - opens[i])


def _upper_shadow(highs: list[float], opens: list[float], closes: list[float], i: int) -> float:
    return highs[i] - max(opens[i], closes[i])


def _lower_shadow(opens: list[float], closes: list[float], lows: list[float], i: int) -> float:
    return min(opens[i], closes[i]) - lows[i]


def _avg_body_abs(opens: list[float], closes: list[float], period: int = 8) -> list[float]:
    """8日滚动平均实体（TA-Lib TA_CANDLEAVGPERIOD=8）。"""
    n = len(opens)
    result = [0.0] * n
    for i in range(period - 1, n):
        window = [_body_abs(opens, closes, j) for j in range(i - period + 1, i + 1)]
        result[i] = sum(window) / period
    return result


# ── 实体/影线状态判定（基于 avg_body） ──

def _is_doji(opens: list[float], closes: list[float], avg: list[float], i: int) -> bool:
    """Body <= avg * 0.08。参考 TA-Lib CandleSetting_BodyDoji = 8%。"""
    return _body_abs(opens, closes, i) <= avg[i] * 0.08 if avg[i] > 0 else False


def _is_short_body(opens: list[float], closes: list[float], avg: list[float], i: int) -> bool:
    """Body <= avg * 0.50。参考 TA-Lib CandleSetting_BodyShort = 50%。"""
    return _body_abs(opens, closes, i) <= avg[i] * 0.50 if avg[i] > 0 else False


def _is_long_body(opens: list[float], closes: list[float], avg: list[float], i: int) -> bool:
    """Body >= avg * 1.50。"""
    return _body_abs(opens, closes, i) >= avg[i] * 1.50 if avg[i] > 0 else False


def _is_upper_shadow_short(highs: list[float], opens: list[float], closes: list[float],
                           avg: list[float], i: int) -> bool:
    return _upper_shadow(highs, opens, closes, i) <= avg[i] * 0.10 if avg[i] > 0 else False


def _is_lower_shadow_short(opens: list[float], closes: list[float], lows: list[float],
                           avg: list[float], i: int) -> bool:
    return _lower_shadow(opens, closes, lows, i) <= avg[i] * 0.10 if avg[i] > 0 else False


def _is_upper_shadow_long(highs: list[float], opens: list[float], closes: list[float],
                          avg: list[float], i: int) -> bool:
    return _upper_shadow(highs, opens, closes, i) >= avg[i] * 1.0 if avg[i] > 0 else False


def _is_lower_shadow_long(opens: list[float], closes: list[float], lows: list[float],
                          avg: list[float], i: int) -> bool:
    return _lower_shadow(opens, closes, lows, i) >= avg[i] * 1.0 if avg[i] > 0 else False


# ── 趋势背景 ──

def _candle_trend_context(opens: list[float], closes: list[float], idx: int,
                          lookback: int = 5) -> str:
    """判断当前 K 线所处的短期趋势背景：up / down / neutral。"""
    if idx < lookback:
        return "neutral"
    yang = sum(1 for j in range(idx - lookback, idx) if closes[j] >= opens[j])
    if yang >= 4:
        return "up"
    elif yang <= 1:
        return "down"
    return "neutral"


# ════════════════════════ 单K线形态 ════════════════════════

def _detect_hammer(h: list[float], l_vals: list[float], o: list[float],
                   c: list[float], avg: list[float], i: int, trend_ctx: str):
    """锤子线：小实体+长下影(>=2x实体)+极短上影+下跌趋势中。"""
    if i < 1:
        return None
    body = _body_abs(o, c, i)
    if body == 0:
        return None
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if ls >= 2.0 * body and us <= body * 0.30 and trend_ctx == "down":
        return ("buy", "锤子线：长下影+低位，空方衰竭多头反击", 4)
    return None


def _detect_shooting_star(h: list[float], l_vals: list[float], o: list[float],
                          c: list[float], avg: list[float], i: int, trend_ctx: str):
    """射击之星：小实体+长上影(>=2x实体)+极短下影+上涨趋势中。"""
    if i < 1:
        return None
    body = _body_abs(o, c, i)
    if body == 0:
        return None
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if us >= 2.0 * body and ls <= body * 0.30 and trend_ctx == "up":
        return ("sell", "射击之星：长上影+高位，多方乏力", 4)
    return None


def _detect_hanging_man(h: list[float], l_vals: list[float], o: list[float],
                        c: list[float], avg: list[float], i: int, trend_ctx: str):
    """上吊线：与锤子线形态一致但出现在上涨趋势中 = 顶部警告。"""
    if i < 1:
        return None
    body = _body_abs(o, c, i)
    if body == 0:
        return None
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if ls >= 2.0 * body and us <= body * 0.30 and trend_ctx == "up":
        return ("sell", "上吊线：上涨后的长下影，诱多出货信号", 4)
    return None


def _detect_inverted_hammer(h: list[float], l_vals: list[float], o: list[float],
                            c: list[float], avg: list[float], i: int, trend_ctx: str):
    """倒锤子：小实体+长上影(>=2x实体)+极短下影+下跌趋势中 = 底部反转前兆。"""
    if i < 1:
        return None
    body = _body_abs(o, c, i)
    if body == 0:
        return None
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if us >= 2.0 * body and ls <= body * 0.30 and trend_ctx == "down":
        return ("buy", "倒锤子：下跌后长上影尝试，多方试探信号", 3)
    return None


def _detect_dragonfly_doji(h: list[float], l_vals: list[float], o: list[float],
                           c: list[float], avg: list[float], i: int, trend_ctx: str):
    """蜻蜓十字：doji实体+长下影(>=3x_total)+极短上影。TA-Lib TA_CDLDRAGONFLYDOJI。"""
    if not _is_doji(o, c, avg, i):
        return None
    total = h[i] - l_vals[i] if h[i] > l_vals[i] else 0.01
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if ls >= 0.6 * total and us <= 0.05 * total:
        if trend_ctx == "down":
            return ("buy", "蜻蜓十字：无实体长下影，空方已无力下压", 5)
        return ("buy", "蜻蜓十字：T字形态，强支撑信号", 3)
    return None


def _detect_gravestone_doji(h: list[float], l_vals: list[float], o: list[float],
                            c: list[float], avg: list[float], i: int, trend_ctx: str):
    """墓碑十字：doji实体+长上影+极短下影。TA-Lib TA_CDLGRAVESTONEDOJI。"""
    if not _is_doji(o, c, avg, i):
        return None
    total = h[i] - l_vals[i] if h[i] > l_vals[i] else 0.01
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if us >= 0.6 * total and ls <= 0.05 * total:
        if trend_ctx == "up":
            return ("sell", "墓碑十字：上影线无实体，上方抛压极重", 5)
        return ("sell", "墓碑十字：墓碑形态，反弹无力信号", 3)
    return None


def _detect_marubozu(o: list[float], c: list[float], h: list[float], l_vals: list[float],
                     avg: list[float], i: int):
    """光头光脚：长实体+极短双影。"""
    if not _is_long_body(o, c, avg, i):
        return None
    us_short = _is_upper_shadow_short(h, o, c, avg, i)
    ls_short = _is_lower_shadow_short(o, c, l_vals, avg, i)
    if us_short and ls_short:
        if c[i] > o[i]:
            return ("buy", "光头光脚阳线：全天单边多，强势", 4)
        else:
            return ("sell", "光头光脚阴线：全天单边空，强势下跌", 4)
    return None


def _detect_spinning_top(o: list[float], c: list[float], h: list[float], l_vals: list[float],
                         avg: list[float], i: int):
    """纺锤线：短实体+长上下影（双影均>=body）。多空激战但无方向。"""
    if not _is_short_body(o, c, avg, i):
        return None
    body = _body_abs(o, c, i)
    us = _upper_shadow(h, o, c, i)
    ls = _lower_shadow(o, c, l_vals, i)
    if us >= body * 0.8 and ls >= body * 0.8:
        return ("caution", "纺锤线：上下影均衡、实体短小，多空胶着等待方向", 2)
    return None


def _detect_bullish_belt_hold(o: list[float], c: list[float], l_vals: list[float],
                              avg: list[float], i: int, trend_ctx: str):
    """看涨捉腰带：开盘=最低（极短下影），大阳线，跌势中出现。"""
    if trend_ctx != "down":
        return None
    if not _is_long_body(o, c, avg, i):
        return None
    if c[i] <= o[i]:
        return None
    ls = _lower_shadow(o, c, l_vals, i)
    if ls <= avg[i] * 0.05 and avg[i] > 0:
        return ("buy", "捉腰带阳线：开盘即最低位的长阳，多方强势进场", 4)
    return None


def _detect_bearish_belt_hold(o: list[float], c: list[float], h: list[float],
                              avg: list[float], i: int, trend_ctx: str):
    """看跌捉腰带：开盘=最高（极短上影），大阴线，涨势中出现。"""
    if trend_ctx != "up":
        return None
    if not _is_long_body(o, c, avg, i):
        return None
    if c[i] >= o[i]:
        return None
    us = _upper_shadow(h, o, c, i)
    if us <= avg[i] * 0.05 and avg[i] > 0:
        return ("sell", "捉腰带阴线：开盘即最高位的长阴，空方强势离场", 4)
    return None


# ════════════════════════ 双K线形态 ════════════════════════

def _detect_bullish_engulfing(o: list[float], c: list[float], avg: list[float],
                              vols: list[float], i: int):
    """看涨吞没：前阴后阳，今日阳线实体完全包住昨日阴线实体。
    参考 TA-Lib TA_CDLENGULFING：前一根短或反方向，后一根长且覆盖前一根。"""
    if i < 1:
        return None
    prev_b = _body(o, c, i - 1)  # signed
    curr_b = _body(o, c, i)
    if prev_b >= 0 or curr_b <= 0:
        return None
    if o[i] >= c[i - 1] or c[i] <= o[i - 1]:
        return None
    # 确认：前阴线实体不能太短（避免噪声），今阳线实体 > 前阴线
    prev_abs = _body_abs(o, c, i - 1)
    curr_abs = _body_abs(o, c, i)
    if curr_abs < prev_abs * 0.9:
        return None  # 吞没程度不够
    vol_confirm = vols[i] > vols[i - 1]
    conf = 5 if vol_confirm else 3
    return ("buy", "看涨吞没：阳线实体完全吞没前阴线" + ("+量确认" if vol_confirm else ""), conf)


def _detect_bearish_engulfing(o: list[float], c: list[float], avg: list[float],
                              vols: list[float], i: int):
    """看跌吞没：前阳后阴，今日阴线实体完全包住昨日阳线实体。"""
    if i < 1:
        return None
    prev_b = _body(o, c, i - 1)
    curr_b = _body(o, c, i)
    if prev_b <= 0 or curr_b >= 0:
        return None
    if o[i] <= c[i - 1] or c[i] >= o[i - 1]:
        return None
    prev_abs = _body_abs(o, c, i - 1)
    curr_abs = _body_abs(o, c, i)
    if curr_abs < prev_abs * 0.9:
        return None
    vol_confirm = vols[i] > vols[i - 1]
    conf = 5 if vol_confirm else 3
    return ("sell", "看跌吞没：阴线实体完全吞噬前阳线" + ("+量确认" if vol_confirm else ""), conf)


def _detect_piercing_line(o: list[float], c: list[float], l_vals: list[float],
                          avg: list[float], vols: list[float], i: int):
    """刺透线（Piercing Line）：前阴后阳，今开盘 < 昨最低（跳空低开），
    今收盘 > 昨阴线中点（穿透 > 50%）。TA-Lib TA_CDLPIERCING。"""
    if i < 1:
        return None
    prev_b = _body(o, c, i - 1)
    curr_b = _body(o, c, i)
    if prev_b >= 0 or curr_b <= 0:
        return None
    if o[i] >= l_vals[i - 1]:
        return None  # 不是跳空低开
    prev_mid = o[i - 1] + c[i - 1]
    if c[i] * 2 <= prev_mid:
        return None  # 收盘没有穿透前阴线50%
    if c[i] >= o[i - 1]:
        return None  # 完全吞没归 engulfing
    conf = 4 if vols[i] > vols[i - 1] else 3
    return ("buy", "刺透线：跳空低开后反转穿透50%+，多方强力反击", conf)


def _detect_dark_cloud_cover(o: list[float], c: list[float], h: list[float],
                             avg: list[float], vols: list[float], i: int):
    """乌云盖顶：前阳后阴，今开盘 > 昨最高（跳空高开），
    今收盘 < 昨阳线中点（下破 > 50%）。TA-Lib TA_CDLDARKCLOUDCOVER。"""
    if i < 1:
        return None
    prev_b = _body(o, c, i - 1)
    curr_b = _body(o, c, i)
    if prev_b <= 0 or curr_b >= 0:
        return None
    if o[i] <= h[i - 1]:
        return None  # 不是跳空高开
    prev_mid = o[i - 1] + c[i - 1]
    if c[i] * 2 >= prev_mid:
        return None  # 收盘没有下破前阳线50%
    if c[i] <= o[i - 1]:
        return None  # 完全吞没归 engulfing
    conf = 4 if vols[i] > vols[i - 1] else 3
    return ("sell", "乌云盖顶：跳空高开后遭抛售下破50%，空方突袭", conf)


def _detect_bullish_harami(o: list[float], c: list[float], h: list[float],
                           l_vals: list[float], avg: list[float], i: int):
    """看涨孕线：前长阴线+今小实体（阳/阴均可），今实体被前全包。
    TA-Lib TA_CDLHARAMI。下跌趋势中。"""
    if i < 1:
        return None
    if _body(o, c, i - 1) >= 0:
        return None
    if not _is_long_body(o, c, avg, i - 1):
        return None
    if not _is_short_body(o, c, avg, i):
        return None
    if h[i] <= max(o[i - 1], c[i - 1]) and l_vals[i] >= min(o[i - 1], c[i - 1]):
        return ("buy", "看涨孕线：大阴后的缩量小K，空方衰竭信号", 4)
    return None


def _detect_bearish_harami(o: list[float], c: list[float], h: list[float],
                           l_vals: list[float], avg: list[float], i: int):
    """看跌孕线：前长阳线+今小实体被全包。上涨趋势中。"""
    if i < 1:
        return None
    if _body(o, c, i - 1) <= 0:
        return None
    if not _is_long_body(o, c, avg, i - 1):
        return None
    if not _is_short_body(o, c, avg, i):
        return None
    if h[i] <= max(o[i - 1], c[i - 1]) and l_vals[i] >= min(o[i - 1], c[i - 1]):
        return ("sell", "看跌孕线：大阳后的缩量小K，多方买盘枯竭", 4)
    return None


def _detect_harami_cross(o: list[float], c: list[float], h: list[float],
                         l_vals: list[float], avg: list[float], i: int):
    """十字孕线：前一天长实体+今天doji被全包。TA-Lib TA_CDLHARAMICROSS。"""
    if i < 1:
        return None
    prev_long = _is_long_body(o, c, avg, i - 1)
    if not prev_long:
        return None
    if not _is_doji(o, c, avg, i):
        return None
    if h[i] <= max(o[i - 1], c[i - 1]) and l_vals[i] >= min(o[i - 1], c[i - 1]):
        if _body(o, c, i - 1) < 0:
            return ("buy", "十字孕线+前阴：大阴后十字星，最强反转信号之一", 5)
        else:
            return ("sell", "十字孕线+前阳：大阳后十字星，涨势终结警告", 5)
    return None


def _detect_tweezers_top(h: list[float], l_vals: list[float], o: list[float],
                         c: list[float], avg: list[float], i: int):
    """平头顶：连续两天最高价相等（误差 < avg_body*5%），上涨趋势中。"""
    if i < 1:
        return None
    if abs(h[i] - h[i - 1]) < avg[i] * 0.05 and avg[i] > 0:
        prev_up = c[i - 1] > o[i - 1]
        if prev_up and _is_short_body(o, c, avg, i):
            return ("sell", "平头顶：两日同高，上方压力确认", 3)
    return None


def _detect_tweezers_bottom(h: list[float], l_vals: list[float], o: list[float],
                            c: list[float], avg: list[float], i: int):
    """平头底：连续两天最低价相等（误差 < avg_body*5%），下跌趋势中。"""
    if i < 1:
        return None
    if abs(l_vals[i] - l_vals[i - 1]) < avg[i] * 0.05 and avg[i] > 0:
        prev_down = c[i - 1] < o[i - 1]
        if prev_down and _is_short_body(o, c, avg, i):
            return ("buy", "平头底：两日同低，下方支撑确认", 3)
    return None


# ════════════════════════ 三K线形态 ════════════════════════

def _detect_three_white_soldiers(o: list[float], c: list[float], h: list[float],
                                 avg: list[float], vols: list[float], i: int):
    """红三兵：连续三根中长阳线，每根开盘在前阳线实体内，收盘在最高附近。
    参考 TA-Lib TA_CDL3WHITESOLDIERS：
    - 三根均为长实体阳线 (Body >= avg_body * 1.2，略宽于Long)
    - 每根开盘在前一根实体内 (not gap up)
    - 每根收盘 near high (上影短)
    - 实体依次增长或至少不萎缩"""
    if i < 2:
        return None
    for j in range(i - 2, i + 1):
        if _body(o, c, j) <= 0:
            return None
        if _body_abs(o, c, j) < avg[j] * 1.2:
            return None
        if not _is_upper_shadow_short(h, o, c, avg, j):
            return None
    if o[i - 1] >= c[i - 2] or o[i - 1] <= o[i - 2]:
        return None
    if o[i] >= c[i - 1] or o[i] <= o[i - 1]:
        return None
    if not (c[i] > c[i - 1] > c[i - 2]):
        return None
    vol_ok = vols[i] > vols[i - 1] and vols[i - 1] > vols[i - 2]
    conf = 5 if vol_ok else 4
    return ("buy", "红三兵：三日梯级上涨" + ("+梯级放量" if vol_ok else ""), conf)


def _detect_three_black_crows(o: list[float], c: list[float], l_vals: list[float],
                              avg: list[float], vols: list[float], i: int):
    """三只乌鸦：连续三根中长阴线，每根开盘在前阴线实体内，收盘在最低附近。
    参考 TA-Lib TA_CDL3BLACKCROWS。"""
    if i < 2:
        return None
    for j in range(i - 2, i + 1):
        if _body(o, c, j) >= 0:
            return None
        if _body_abs(o, c, j) < avg[j] * 1.2:
            return None
        if not _is_lower_shadow_short(o, c, l_vals, avg, j):
            return None
    if o[i - 1] <= c[i - 2] or o[i - 1] >= o[i - 2]:
        return None
    if o[i] <= c[i - 1] or o[i] >= o[i - 1]:
        return None
    if not (c[i] < c[i - 1] < c[i - 2]):
        return None
    vol_ok = vols[i] > vols[i - 1] and vols[i - 1] > vols[i - 2]
    conf = 5 if vol_ok else 4
    return ("sell", "三只乌鸦：三日梯级下跌" + ("+梯级放量" if vol_ok else ""), conf)


def _detect_morning_star(o: list[float], c: list[float], h: list[float],
                         l_vals: list[float], avg: list[float], i: int):
    """启明星（晨星）：长阴线 + 跳空低开小实体 + 跳空高开长阳线。
    参考 TA-Lib TA_CDLMORNINGSTAR：
    - Day1: 长阴 (body >= avg*1.0)
    - Day2: 短实体 (body <= avg*0.5)，跳空低开 vs Day1 收盘
    - Day3: 长阳，跳空高开 vs Day2 收盘，收盘切入 Day1 阴线至少 30%"""
    if i < 2:
        return None
    if _body(o, c, i - 2) >= 0:
        return None
    if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
        return None
    if not _is_short_body(o, c, avg, i - 1):
        return None
    if h[i - 1] >= c[i - 2]:
        return None
    if _body(o, c, i) <= 0:
        return None
    if _body_abs(o, c, i) < avg[i] * 1.0:
        return None
    if l_vals[i] <= h[i - 1]:
        return None
    day1_range = _body_abs(o, c, i - 2)
    if day1_range > 0:
        penetration = (c[i] - o[i - 2]) / day1_range
        if penetration < 0.3:
            return None
    return ("buy", "启明星：长阴+星+长阳三线反转，底部确认最强信号之一", 5)


def _detect_evening_star(o: list[float], c: list[float], h: list[float],
                         l_vals: list[float], avg: list[float], i: int):
    """黄昏星：长阳线 + 跳空高开小实体 + 跳空低开长阴线。
    参考 TA-Lib TA_CDLEVENINGSTAR。"""
    if i < 2:
        return None
    if _body(o, c, i - 2) <= 0:
        return None
    if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
        return None
    if not _is_short_body(o, c, avg, i - 1):
        return None
    if l_vals[i - 1] <= c[i - 2]:
        return None
    if _body(o, c, i) >= 0:
        return None
    if _body_abs(o, c, i) < avg[i] * 1.0:
        return None
    if h[i] >= l_vals[i - 1]:
        return None
    day1_range = _body_abs(o, c, i - 2)
    if day1_range > 0:
        penetration = (o[i - 2] - c[i]) / day1_range
        if penetration < 0.3:
            return None
    return ("sell", "黄昏星：长阳+星+长阴三线见顶，顶部反转最强信号之一", 5)


def _detect_morning_doji_star(o: list[float], c: list[float], h: list[float],
                              l_vals: list[float], avg: list[float], i: int):
    """启明星十字版：Day2 为 doji 的启明星，比普通启明星更强。TA-Lib TA_CDLMORNINGDOJISTAR。"""
    if i < 2:
        return None
    if _body(o, c, i - 2) >= 0:
        return None
    if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
        return None
    if not _is_doji(o, c, avg, i - 1):
        return None
    if h[i - 1] >= c[i - 2]:
        return None
    if _body(o, c, i) <= 0:
        return None
    if _body_abs(o, c, i) < avg[i] * 1.0:
        return None
    if l_vals[i] <= h[i - 1]:
        return None
    return ("buy", "启明十字星：长阴+十字+长阳，极致底部反转", 5)


def _detect_evening_doji_star(o: list[float], c: list[float], h: list[float],
                              l_vals: list[float], avg: list[float], i: int):
    """黄昏星十字版：Day2 为 doji。TA-Lib TA_CDLEVENINGDOJISTAR。"""
    if i < 2:
        return None
    if _body(o, c, i - 2) <= 0:
        return None
    if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
        return None
    if not _is_doji(o, c, avg, i - 1):
        return None
    if l_vals[i - 1] <= c[i - 2]:
        return None
    if _body(o, c, i) >= 0:
        return None
    if _body_abs(o, c, i) < avg[i] * 1.0:
        return None
    if h[i] >= l_vals[i - 1]:
        return None
    return ("sell", "黄昏十字星：长阳+十字+长阴，极致顶部反转", 5)


def _detect_abandoned_baby(o: list[float], c: list[float], h: list[float],
                           l_vals: list[float], avg: list[float], i: int, bull: bool):
    """弃婴形态：doji 星两端都跳空（比普通晨/昏星多一个gap），最极端的反转形态。
    TA-Lib TA_CDLABANDONEDBABY。"""
    if i < 2:
        return None
    if not _is_doji(o, c, avg, i - 1):
        return None
    if bull:
        if _body(o, c, i - 2) >= 0:
            return None
        if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
            return None
        if h[i - 1] >= l_vals[i - 2]:
            return None
        if _body(o, c, i) <= 0:
            return None
        if l_vals[i] <= h[i - 1]:
            return None
        return ("buy", "底部弃婴：doji完全孤立在两根长阴长阳之间，底部反转极端信号", 5)
    else:
        if _body(o, c, i - 2) <= 0:
            return None
        if _body_abs(o, c, i - 2) < avg[i - 2] * 1.0:
            return None
        if l_vals[i - 1] <= h[i - 2]:
            return None
        if _body(o, c, i) >= 0:
            return None
        if h[i] >= l_vals[i - 1]:
            return None
        return ("sell", "顶部弃婴：doji完全孤立，顶部反转极端信号", 5)


# ════════════════════════ 多K线延续形态 ════════════════════════

def _detect_rising_three_methods(o: list[float], c: list[float], h: list[float],
                                 l_vals: list[float], avg: list[float], i: int):
    """上升三法：Day1长阳 + Day2-4三根小K缩回不破Day1底部 + Day5再长阳突破。
    TA-Lib TA_CDLRISING3METHODS。要求：5天窗口。"""
    if i < 4:
        return None
    if _body(o, c, i - 4) <= 0:
        return None
    if _body_abs(o, c, i - 4) < avg[i - 4] * 1.5:
        return None
    day1_body = c[i - 4] - o[i - 4]
    if day1_body <= 0:
        return None
    for j in range(i - 3, i):
        if not _is_short_body(o, c, avg, j):
            return None
        if l_vals[j] < min(o[i - 4], c[i - 4]):
            return None
        if h[j] > max(o[i - 4], c[i - 4]):
            return None
    if _body(o, c, i) <= 0:
        return None
    if c[i] <= h[i - 4]:
        return None
    return ("buy", "上升三法：长阳+三小K休整+再长阳突破，上涨中继", 4)


def _detect_falling_three_methods(o: list[float], c: list[float], h: list[float],
                                  l_vals: list[float], avg: list[float], i: int):
    """下降三法：Day1长阴 + Day2-4三根小反弹 + Day5再长阴。TA-Lib TA_CDLFALLING3METHODS。"""
    if i < 4:
        return None
    if _body(o, c, i - 4) >= 0:
        return None
    if _body_abs(o, c, i - 4) < avg[i - 4] * 1.5:
        return None
    for j in range(i - 3, i):
        if not _is_short_body(o, c, avg, j):
            return None
        if h[j] > max(o[i - 4], c[i - 4]):
            return None
        if l_vals[j] < min(o[i - 4], c[i - 4]):
            return None
    if _body(o, c, i) >= 0:
        return None
    if c[i] >= l_vals[i - 4]:
        return None
    return ("sell", "下降三法：长阴+三小K反弹+再长阴，下跌中继", 4)


# ════════════════════════ 1. 蜡烛眼 (博弈·情绪) —— 深挖版 ════════════════════════

def candle_eye(daily_data: list[dict]) -> EyeVerdict:
    closes = [float(r["close"]) for r in daily_data]
    opens  = [float(r["open"]) for r in daily_data]
    highs  = [float(r["high"]) for r in daily_data]
    lows   = [float(r["low"]) for r in daily_data]
    vols   = [float(r.get("volume", 0) or 0) for r in daily_data]
    n = len(daily_data)

    avg = _avg_body_abs(opens, closes)
    i = n - 1

    # ── trend: 多周期阳线占比+方向连续 ──
    def _yang_streak(start: int, end: int) -> tuple[int, int]:
        s, mx = 0, 0
        for j in range(start, end):
            if closes[j] >= opens[j]:
                s += 1
                mx = max(mx, s)
            else:
                s = 0
        return mx, s

    short_lb = min(5, n)
    mid_lb  = min(10, n)
    long_lb = min(20, n)

    s5p  = sum(1 for j in range(n - short_lb, n) if closes[j] >= opens[j]) / short_lb * 100
    m10p = sum(1 for j in range(n - mid_lb, n) if closes[j] >= opens[j]) / mid_lb * 100
    l20p = sum(1 for j in range(n - long_lb, n) if closes[j] >= opens[j]) / long_lb * 100

    # 连续阳线检测
    streak_max, streak_cur = _yang_streak(n - mid_lb, n)
    curr_body = _body_abs(opens, closes, i)

    # 加权综合趋势分
    trend_score = (s5p * 0.5 + m10p * 0.3 + l20p * 0.2 - 50)  # -100~+100
    if curr_body > 0 and avg[i] > 0:
        trend_score += (curr_body / avg[i] - 1) * 15  # 实体>均值加正向分

    if trend_score > 15:
        trend, trend_detail = "up", f"5d{s5p:.0f}%/10d{m10p:.0f}%/20d{l20p:.0f}%阳线，多头主导"
        if streak_cur >= 3:
            trend_detail += f"，连阳{streak_cur}日加速"
    elif trend_score < -15:
        trend, trend_detail = "down", f"5d{s5p:.0f}%/10d{m10p:.0f}%/20d{l20p:.0f}%阳线，空头主导"
        if streak_cur < 0:
            trend_detail += f"，连阴{abs(streak_cur)}日"
    else:
        trend, trend_detail = "neutral", f"5d{s5p:.0f}%/10d{m10p:.0f}%/20d{l20p:.0f}%，多空拉锯"

    # ── position: 支撑阻力关键位 — 基于最近N日的高低点枢轴 ──
    high20 = max(highs[max(0, n - 20):n]) if n >= 5 else highs[i]
    low20  = min(lows[max(0, n - 20):n]) if n >= 5 else lows[i]
    high5  = max(highs[max(0, n - 5):n]) if n >= 3 else highs[i]
    low5   = min(lows[max(0, n - 5):n]) if n >= 3 else lows[i]

    dist_from_high5 = (high5 - closes[i]) / high5 * 100 if high5 > 0 else 100
    dist_from_low5  = (closes[i] - low5) / low5 * 100 if low5 > 0 else 100
    dist_from_high20 = (high20 - closes[i]) / high20 * 100 if high20 > 0 else 100
    dist_from_low20  = (closes[i] - low20) / low20 * 100 if low20 > 0 else 100

    # 博弈激烈度：实体vs平均实体+影线长度
    if avg[i] > 0:
        body_ratio = curr_body / avg[i]
    else:
        body_ratio = 1.0

    us_len = _upper_shadow(highs, opens, closes, i)
    ls_len = _lower_shadow(opens, closes, lows, i)

    position_details: list[str] = []
    if dist_from_high5 < 1.5:
        position_details.append(f"触5日高{high5:.2f}(距{dist_from_high5:.1f}%)，阻力位")
    if dist_from_low5 < 1.5:
        position_details.append(f"触5日低{low5:.2f}(距{dist_from_low5:.1f}%)，支撑位")
    if dist_from_high20 < 2.0 and dist_from_high5 >= 1.5:
        position_details.append(f"靠近20日高{high20:.2f}(距{dist_from_high20:.1f}%)")
    if dist_from_low20 < 2.0 and dist_from_low5 >= 1.5:
        position_details.append(f"靠近20日低{low20:.2f}(距{dist_from_low20:.1f}%)")

    if body_ratio > 1.8:
        position_details.append(f"实体{body_ratio:.1f}x均值，多空激烈博弈")
    elif body_ratio > 1.2:
        position_details.append(f"实体{body_ratio:.1f}x均值，博弈升温")

    if us_len > avg[i] * 1.5 and avg[i] > 0:
        position_details.append("长上影：上方抛压重")
    if ls_len > avg[i] * 1.5 and avg[i] > 0:
        position_details.append("长下影：下方承接强")

    # Simplify: check if any key level was hit
    near_5high = dist_from_high5 < 1.5
    near_5low = dist_from_low5 < 1.5
    near_20 = dist_from_high20 < 2.0 or dist_from_low20 < 2.0

    if near_5high or near_5low:
        position, position_detail = "key_level", "；".join(position_details) if position_details else "无特殊博弈"
    elif near_20 or body_ratio > 1.5:
        position, position_detail = "approaching", "；".join(position_details) if position_details else "接近关键区"
    else:
        position, position_detail = "mid_range", "；".join(position_details) if position_details else "中段运行，博弈平静"

    # ── signal: 20+形态库优先扫描 ──
    trend_ctx = _candle_trend_context(opens, closes, i)

    # 扫描所有形态，收集结果（按优先级+置信度排序）
    candidates: list[tuple[str, str, int, int]] = []  # (signal, detail, conf, priority)

    # 优先级定义：0=极端反转 1=强反转 2=普通反转 3=延续 4=提示
    PATTERNS = [
        # ── 优先级0: 弃婴（最强） ──
        (lambda: _detect_abandoned_baby(opens, closes, highs, lows, avg, i, True), 0),
        (lambda: _detect_abandoned_baby(opens, closes, highs, lows, avg, i, False), 0),
        # ── 优先级0: 晨/昏十字星 ──
        (lambda: _detect_morning_doji_star(opens, closes, highs, lows, avg, i), 0),
        (lambda: _detect_evening_doji_star(opens, closes, highs, lows, avg, i), 0),
        # ── 优先级1: 晨/昏星 ──
        (lambda: _detect_morning_star(opens, closes, highs, lows, avg, i), 1),
        (lambda: _detect_evening_star(opens, closes, highs, lows, avg, i), 1),
        # ── 优先级1: 红三兵/三鸦 ──
        (lambda: _detect_three_white_soldiers(opens, closes, highs, avg, vols, i), 1),
        (lambda: _detect_three_black_crows(opens, closes, lows, avg, vols, i), 1),
        # ── 优先级1: 吞没 ──
        (lambda: _detect_bullish_engulfing(opens, closes, avg, vols, i), 1),
        (lambda: _detect_bearish_engulfing(opens, closes, avg, vols, i), 1),
        # ── 优先级2: 刺透/乌云盖顶 ──
        (lambda: _detect_piercing_line(opens, closes, lows, avg, vols, i), 2),
        (lambda: _detect_dark_cloud_cover(opens, closes, highs, avg, vols, i), 2),
        # ── 优先级2: 十字孕线（Harami Cross） ──
        (lambda: _detect_harami_cross(opens, closes, highs, lows, avg, i), 2),
        # ── 优先级2: 上升/下降三法 ──
        (lambda: _detect_rising_three_methods(opens, closes, highs, lows, avg, i), 3),
        (lambda: _detect_falling_three_methods(opens, closes, highs, lows, avg, i), 3),
        # ── 优先级3: 孕线 ──
        (lambda: _detect_bullish_harami(opens, closes, highs, lows, avg, i), 3),
        (lambda: _detect_bearish_harami(opens, closes, highs, lows, avg, i), 3),
        # ── 优先级3: 蜻蜓/墓碑十字 ──
        (lambda: _detect_dragonfly_doji(highs, lows, opens, closes, avg, i, trend_ctx), 3),
        (lambda: _detect_gravestone_doji(highs, lows, opens, closes, avg, i, trend_ctx), 3),
        # ── 优先级4: 锤子/上吊/射击之星/倒锤子 ──
        (lambda: _detect_hammer(highs, lows, opens, closes, avg, i, trend_ctx), 4),
        (lambda: _detect_shooting_star(highs, lows, opens, closes, avg, i, trend_ctx), 4),
        (lambda: _detect_hanging_man(highs, lows, opens, closes, avg, i, trend_ctx), 4),
        (lambda: _detect_inverted_hammer(highs, lows, opens, closes, avg, i, trend_ctx), 4),
        # ── 优先级4: 捉腰带 ──
        (lambda: _detect_bullish_belt_hold(opens, closes, lows, avg, i, trend_ctx), 4),
        (lambda: _detect_bearish_belt_hold(opens, closes, highs, avg, i, trend_ctx), 4),
        # ── 优先级4: 光头光脚 ──
        (lambda: _detect_marubozu(opens, closes, highs, lows, avg, i), 4),
        # ── 优先级5: 纺锤（弱信号） ──
        (lambda: _detect_spinning_top(opens, closes, highs, lows, avg, i), 5),
        # ── 优先级5: 平头 ──
        (lambda: _detect_tweezers_top(highs, lows, opens, closes, avg, i), 5),
        (lambda: _detect_tweezers_bottom(highs, lows, opens, closes, avg, i), 5),
    ]

    for detector, pri in PATTERNS:
        result = detector()
        if result is not None:
            candidates.append((result[0], result[1], result[2], pri))

    # 选择最优：最低优先级(数字越小越强)，同优先级选最高置信度
    if candidates:
        candidates.sort(key=lambda x: (x[3], -x[2]))  # 先按优先级再按置信度降序
        best = candidates[0]
        signal, signal_detail, confidence = best[0], best[1], best[2]
        # 如果有其他高置信候选(conf>=4)，也记录
        others = [c for c in candidates if c[2] >= 4 and c != best]
        if others:
            signal_detail += f"（另见{'，'.join(c[1][:8] for c in others[:2])}）"
    else:
        # 无形态匹配 → 通用十字星提示
        if _is_doji(opens, closes, avg, i):
            signal, signal_detail = "caution", "十字星：多空完全平衡，即将变盘"
            confidence = 2
        else:
            signal, signal_detail = "none", "当前无经典蜡烛形态"
            confidence = 1

    return EyeVerdict(
        eye="candle", lens="博弈·情绪",
        trend=trend, trend_detail=trend_detail,
        position=position, position_detail=position_detail,
        signal=signal, signal_detail=signal_detail,
        confidence=confidence,
        horizon="short",  # 形态1-5日兑现，博弈信号不过夜
    )


# ════════════════════════ 2. 指标眼 (数据·异常) —— 深挖版 ════════════════════════
#
# 核心升级：
#   1. 多周期背离检测：peak/trough 方法，不用简单两点比较
#   2. 背离类型：普通背离 (regular) + 隐藏背离 (hidden)
#   3. RSI Failure Swing：Cardwell 定义的顶点确认信号
#   4. 布林挤压：带宽跟125日历史百分位比（不固定3%阈值）
#   5. KDJ 钝化：检测 K/D 持续停留在超买/超卖区的天数
#   6. 多指标共振加权：5指标加权趋势分，替代简单3票制

# ── 局部极值检测 ──

def _find_peaks(a: list[float], order: int = 5) -> list[int]:
    """找出局部峰值索引（价格新高对应的index）。order=左右各需多少根确认。"""
    n = len(a)
    peaks: list[int] = []
    for i in range(order, n - order):
        if all(a[i] >= a[i - k] for k in range(1, order + 1)) and \
           all(a[i] >= a[i + k] for k in range(1, order + 1)):
            peaks.append(i)
    return peaks


def _find_troughs(a: list[float], order: int = 5) -> list[int]:
    """找出局部谷底索引。"""
    n = len(a)
    troughs: list[int] = []
    for i in range(order, n - order):
        if all(a[i] <= a[i - k] for k in range(1, order + 1)) and \
           all(a[i] <= a[i + k] for k in range(1, order + 1)):
            troughs.append(i)
    return troughs


# ── MACD 背离检测（peak/trough 方法） ──

def _detect_macd_divergence(closes: list[float], dif: list[float], n: int):
    """用价格和 DIF 峰谷对比检测背离。
    参考 MACD 经典背离定义：
    - 顶背离 (bearish divergence): 价格 HH + DIF LH → sell
    - 底背离 (bullish divergence): 价格 LL + DIF HL → buy
    - 隐藏顶背离 (hidden bearish): 价格 LH + DIF HH → 趋势延续确认
    - 隐藏底背离 (hidden bullish): 价格 HL + DIF LL → 趋势延续确认
    使用最近 60 根 bar 的峰谷进行检测。"""
    lookback = min(60, n)
    start = n - lookback

    price_peaks = _find_peaks(closes[start:], order=3)
    price_troughs = _find_troughs(closes[start:], order=3)
    dif_peaks = _find_peaks(dif[start:], order=3)
    dif_troughs = _find_troughs(dif[start:], order=3)

    results: list[tuple[str, str, int]] = []  # (signal, detail, conf)

    # ── 普通顶背离：价格 HH + DIF LH ──
    if len(price_peaks) >= 2 and len(dif_peaks) >= 2:
        p1, p2 = price_peaks[-2], price_peaks[-1]
        closest_dif = [d for d in dif_peaks if abs(d - p2) <= 5]
        prev_dif = [d for d in dif_peaks if abs(d - p1) <= 5]
        if closest_dif and prev_dif:
            price_higher = closes[start + p2] > closes[start + p1]
            dif_lower = dif[start + closest_dif[-1]] < dif[start + prev_dif[-1]] * 0.95
            if price_higher and dif_lower:
                results.append(("sell",
                    f"MACD顶背离：价格新高({closes[start+p2]:.2f}>{closes[start+p1]:.2f})但DIF在降，上涨动能衰竭", 5))

    # ── 普通底背离：价格 LL + DIF HL ──
    if len(price_troughs) >= 2 and len(dif_troughs) >= 2:
        p1, p2 = price_troughs[-2], price_troughs[-1]
        closest_dif = [d for d in dif_troughs if abs(d - p2) <= 5]
        prev_dif = [d for d in dif_troughs if abs(d - p1) <= 5]
        if closest_dif and prev_dif:
            price_lower = closes[start + p2] < closes[start + p1]
            dif_higher = dif[start + closest_dif[-1]] > dif[start + prev_dif[-1]] * 1.05
            if price_lower and dif_higher:
                results.append(("buy",
                    f"MACD底背离：价格新低({closes[start+p2]:.2f}<{closes[start+p1]:.2f})但DIF在升，空方力竭", 5))

    # ── 隐藏顶背离（HBD）：价格 LH + DIF HH → 下跌中继 ──
    if len(price_peaks) >= 2 and len(dif_peaks) >= 2:
        p1, p2 = price_peaks[-2], price_peaks[-1]
        closest_dif = [d for d in dif_peaks if abs(d - p2) <= 5]
        prev_dif = [d for d in dif_peaks if abs(d - p1) <= 5]
        if closest_dif and prev_dif:
            price_lower = closes[start + p2] < closes[start + p1]
            dif_higher2 = dif[start + closest_dif[-1]] > dif[start + prev_dif[-1]] * 1.05
            if price_lower and dif_higher2:
                # 确认趋势向下才判断为 hidden bearish
                ma20 = sum(closes[-20:]) / 20 if n >= 20 else closes[-1]
                if closes[-1] < ma20:
                    results.append(("sell",
                        f"MACD隐藏顶背离：价格反弹无力+DIF背离，下跌中继", 4))

    # ── 隐藏底背离（HBD）：价格 HL + DIF LL → 上涨中继 ──
    if len(price_troughs) >= 2 and len(dif_troughs) >= 2:
        p1, p2 = price_troughs[-2], price_troughs[-1]
        closest_dif = [d for d in dif_troughs if abs(d - p2) <= 5]
        prev_dif = [d for d in dif_troughs if abs(d - p1) <= 5]
        if closest_dif and prev_dif:
            price_higher2 = closes[start + p2] > closes[start + p1]
            dif_lower2 = dif[start + closest_dif[-1]] < dif[start + prev_dif[-1]] * 0.95
            if price_higher2 and dif_lower2:
                ma20 = sum(closes[-20:]) / 20 if n >= 20 else closes[-1]
                if closes[-1] > ma20:
                    results.append(("buy",
                        f"MACD隐藏底背离：价格回调不破+DIF隐藏底背，上涨中继", 4))

    return results


# ── RSI Failure Swing（Cardwell 经典用法） ──

def _detect_rsi_failure_swing(rsi14: list[float], n: int):
    """RSI Failure Swing：比纯超买/超卖更精确的顶部/底部信号。
    顶部 Failure Swing：
      RSI > 70 → 回落到 70 以下 → 再次反弹但不过 70 → 跌破前低 → sell
    底部 Failure Swing：
      RSI < 30 → 反弹到 30 以上 → 再次回落在 30 以上 → 突破前高 → buy"""
    results: list[tuple[str, str, int]] = []

    if n < 20:
        return results

    # 顶 Failure Swing：需要 RSI 曾 > 70 然后回落
    recent = rsi14[-20:]
    # 找最近一次 RSI > 70
    above70_indices = [i for i in range(15) if recent[i] > 70 and i + 5 < len(recent)]
    for ai in above70_indices[-2:]:  # 只看最近两次
        # 回落 < 70
        after = recent[ai+1:]
        drop_to = min(after[:3]) if len(after) >= 3 else 100
        if drop_to < 70:
            # 再次反弹
            rebound_peak = max(after[3:8]) if len(after) >= 8 else 0
            if 65 < rebound_peak < 72:
                # 再次跌破
                final = after[8:13] if len(after) >= 13 else after[3:]
                if final and min(final) < drop_to:
                    results.append(("sell",
                        "RSI Failure Swing顶：超买→回落→反弹不过70→破前低，顶部确认", 5))
                    break

    # 底 Failure Swing
    below30_indices = [i for i in range(15) if recent[i] < 30 and i + 5 < len(recent)]
    for bi in below30_indices[-2:]:
        after = recent[bi+1:]
        rise_to = max(after[:3]) if len(after) >= 3 else 0
        if rise_to > 30:
            rebound_low = min(after[3:8]) if len(after) >= 8 else 100
            if 28 < rebound_low < 35:
                final = after[8:13] if len(after) >= 13 else after[3:]
                if final and max(final) > rise_to:
                    results.append(("buy",
                        "RSI Failure Swing底：超卖→反弹→回落不破30→破前高，底部确认", 5))
                    break

    return results


# ── RSI 多周期背离 ──

def _detect_rsi_divergence_mtf(closes: list[float], rsi14: list[float], n: int):
    """RSI 背离 + 多周期确认：
    - 用峰谷检测替代简单两点比较（同 MACD 方法）
    - 区分 regular / hidden divergence"""
    results: list[tuple[str, str, int]] = []
    lookback = min(60, n)
    start = n - lookback

    price_peaks = _find_peaks(closes[start:], order=3)
    price_troughs = _find_troughs(closes[start:], order=3)
    rsi_peaks = _find_peaks(rsi14[start:], order=3)
    rsi_troughs = _find_troughs(rsi14[start:], order=3)

    # 顶背离
    if len(price_peaks) >= 2 and len(rsi_peaks) >= 2:
        p1, p2 = price_peaks[-2], price_peaks[-1]
        rsi_match = [r for r in rsi_peaks if abs(r - p2) <= 5]
        rsi_prev = [r for r in rsi_peaks if abs(r - p1) <= 5]
        if rsi_match and rsi_prev:
            if closes[start + p2] > closes[start + p1] and rsi14[start + rsi_match[-1]] < rsi14[start + rsi_prev[-1]]:
                results.append(("sell",
                    "RSI顶背离：价创新高但RSI走低，涨势减弱", 5))

    # 底背离
    if len(price_troughs) >= 2 and len(rsi_troughs) >= 2:
        p1, p2 = price_troughs[-2], price_troughs[-1]
        rsi_match = [r for r in rsi_troughs if abs(r - p2) <= 5]
        rsi_prev = [r for r in rsi_troughs if abs(r - p1) <= 5]
        if rsi_match and rsi_prev:
            if closes[start + p2] < closes[start + p1] and rsi14[start + rsi_match[-1]] > rsi14[start + rsi_prev[-1]]:
                results.append(("buy",
                    "RSI底背离：价创新低但RSI走高，跌势衰竭", 5))

    return results


# ── 布林挤压（percentile 方法） ──

def _bollinger_squeeze_analysis(closes: list[float], n: int):
    """布林带挤压检测：带宽相对125日百分位，不是固定3%。
    参考 Bollinger 本人定义：
    - Squeeze: 带宽在 6 个月最低（即 125 日 5th percentile）
    - 带宽 = (upper - lower) / mid * 100
    - 挤压后第一个突破中轨的阳线 = signal"""
    results: list[tuple[str, str, int]] = []
    if n < 20:
        return results

    from app.services.factor_lib import bollinger as boll
    bb = boll(closes, 20, 2.0)
    mids = bb["mid"]
    uppers = bb["upper"]
    lowers = bb["lower"]

    recent_mid = _safe(mids, n - 1, 0)
    recent_up = _safe(uppers, n - 1, 0)
    recent_lo = _safe(lowers, n - 1, 0)
    if recent_mid <= 0:
        return results

    current_width = (recent_up - recent_lo) / recent_mid * 100

    # 计算每根 bar 的带宽
    widths: list[float] = []
    for i in range(len(mids)):
        up = _safe(uppers, i, 0)
        lo = _safe(lowers, i, 0)
        mid = _safe(mids, i, 0)
        if mid > 0 and up > 0 and lo > 0:
            w = (up - lo) / mid * 100
            widths.append(w)
        else:
            widths.append(5.0)

    # 125 日百分位
    hist_period = min(125, len(widths))
    hist_widths = widths[-hist_period:]
    hist_widths.sort()

    # 获取分位数
    def _percentile(sorted_list: list[float], pct: float) -> float:
        idx = int(len(sorted_list) * pct / 100)
        idx = max(0, min(len(sorted_list) - 1, idx))
        return sorted_list[idx]

    p10 = _percentile(hist_widths, 10)
    p25 = _percentile(hist_widths, 25)
    p50 = _percentile(hist_widths, 50)

    # 最新收盘 vs 布林中轨
    last_close = closes[-1] if n > 0 else 0

    if current_width <= p10:
        # 极端挤压 → 能量积蓄
        direction = "向上" if last_close > recent_mid else "向下"
        results.append(("caution" if direction == "向下" else "buy",
            f"布林极限挤压：带宽{current_width:.1f}%（<P10={p10:.1f}%），即将剧烈变盘{direction}",
            4))
    elif current_width <= p25:
        results.append(("caution",
            f"布林收缩：带宽{current_width:.1f}%（<P25={p25:.1f}%），波动率偏低", 2))

    # 突破信号：价格突破上下轨 + 放出阳量
    if last_close > recent_up:
        results.append(("buy",
            f"布林突破上轨{recent_up:.2f}，趋势加速信号", 4 if current_width <= p25 else 3))
    elif last_close < recent_lo:
        results.append(("sell",
            f"布林跌破下轨{recent_lo:.2f}，加速下跌信号", 4 if current_width <= p25 else 3))

    return results


# ── KDJ 钝化检测 ──

def _kdj_stagnation_analysis(highs: list[float], lows: list[float], closes: list[float], n: int):
    """KDJ 钝化：K/D 在超买区(>80)或超卖区(<20)持续停留。
    钝化本质是趋势太强、指标失效——但钝化本身是强势信号。
    - 高位钝化(>80持续>5天)：强趋势中，不是卖出时机，反而是持有信号
    - 低位钝化(<20持续>5天)：弱趋势中，不是买入时机，反而是观望信号
    - 钝化解除（K/D回到中间区域）：真正的转折信号"""
    results: list[tuple[str, str, int]] = []
    if n < 14:
        return results

    kdj_r = kdj(highs, lows, closes, 9)
    k_vals = kdj_r["k"]
    d_vals = kdj_r["d"]
    j_vals = kdj_r["j"]

    # 统计最近 K 在超买/超卖区域的连续天数
    i = len(k_vals) - 1
    ovb_days = 0   # >80 连续天数
    ovs_days = 0   # <20 连续天数
    for j in range(i, max(0, i - 30), -1):
        kv = _safe(k_vals, j, 50)
        if kv > 80:
            ovb_days += 1
            ovs_days = 0
        elif kv < 20:
            ovs_days += 1
            ovb_days = 0
        else:
            break

    kv_now = _safe(k_vals, i, 50)
    dv_now = _safe(d_vals, i, 50)
    jv_now = _safe(j_vals, i, 50)

    if ovb_days >= 8:
        results.append(("caution",
            f"KDJ高位钝化：K>{kv_now:.0f}持续{ovb_days}天，趋势强但随时翻转", 4))
    elif ovb_days >= 5:
        results.append(("caution",
            f"KDJ超买区停留{ovb_days}天，不建议逆势做空", 2))
    elif ovs_days >= 8:
        results.append(("caution",
            f"KDJ低位钝化：K<{kv_now:.0f}持续{ovs_days}天，弱趋势但随时反弹", 4))
    elif ovs_days >= 5:
        results.append(("caution",
            f"KDJ超卖区停留{ovs_days}天，不建议逆势抄底", 2))
    else:
        # 无钝化 → 经典金叉死叉
        kv_prev = _safe(k_vals, i - 1, 50)
        dv_prev = _safe(d_vals, i - 1, 50)
        # 金叉：K 上穿 D
        if kv_prev <= dv_prev and kv_now > dv_now and kv_now < 50:
            results.append(("buy",
                f"KDJ金叉 K={kv_now:.0f}>D={dv_now:.0f} 中低位，反弹信号", 4))
        # 死叉：K 下穿 D
        elif kv_prev >= dv_prev and kv_now < dv_now and kv_now > 50:
            results.append(("sell",
                f"KDJ死叉 K={kv_now:.0f}<D={dv_now:.0f} 中高位，回落信号", 4))

    return results


# ── 多指标共振趋势（加权评分） ──

def _indicator_trend_score(closes: list[float], dif: list[float], rsi14: list[float],
                           ma20: list[float], ma60: list[float], n: int) -> tuple[str, str]:
    """5 指标加权评分决议趋势：
    MACD(权重3) + RSI(权重2) + MA20(权重2) + MA60(权重1) + DIF/DEA交叉(权重1)
    总分 -9 ~ +9，正=up，负=down，接近0=neutral"""
    i = n - 1
    last_close = closes[i] if n > 0 else 0

    # MACD DIF sign (weight 3)
    last_dif = _safe(dif, i)
    dif_score = 3 if last_dif > 0 else (-3 if last_dif < 0 else 0)

    # RSI zone (weight 2)
    last_rsi = _safe(rsi14, i, 50)
    if last_rsi > 60:
        rsi_score = 2
    elif last_rsi > 50:
        rsi_score = 1
    elif last_rsi > 40:
        rsi_score = -1
    else:
        rsi_score = -2

    # MA20 位置 (weight 2)
    last_ma20 = _safe(ma20, i, last_close)
    ma20_score = 2 if last_close > last_ma20 else (-2 if last_close < last_ma20 else 0)

    # MA60 位置 (weight 1)
    last_ma60 = _safe(ma60, i, last_close)
    ma60_score = 1 if last_close > last_ma60 else (-1 if last_close < last_ma60 else 0)

    # DIF/DEA 金叉死叉 (weight 1)
    mc = macd(closes)
    dea = mc["dea"]
    dif_prev = _safe(dif, i - 3)
    dea_prev = _safe(dea, i - 3)
    dif_now_val = _safe(dif, i)
    dea_now_val = _safe(dea, i)
    cross_score = 0
    if dif_prev <= dea_prev and dif_now_val > dea_now_val:
        cross_score = 1  # 金叉
    elif dif_prev >= dea_prev and dif_now_val < dea_now_val:
        cross_score = -1  # 死叉

    # 均线排列 (weight 1 bonus)
    ma5 = sma(closes, 5)
    last_ma5 = _safe(ma5, i, last_close)
    alignment = 0
    if last_ma5 > last_ma20 > last_ma60:
        alignment = 2
    elif last_ma5 < last_ma20 < last_ma60:
        alignment = -2

    total = dif_score + rsi_score + ma20_score + ma60_score + cross_score + alignment

    if total >= 4:
        trend = "up"
        detail = (f"强多头共振(评分{total})：DIF>0+RSI{last_rsi:.0f}+价>MA20/60"
                  f"{'+均线多头排列' if alignment > 0 else ''}")
    elif total >= 1:
        trend = "up"
        detail = (f"偏多共振(评分{total})：DIF>0+RSI{last_rsi:.0f}+价>MA20"
                  f"{'+金叉' if cross_score > 0 else ''}")
    elif total <= -4:
        trend = "down"
        detail = (f"强空头共振(评分{total})：DIF<0+RSI{last_rsi:.0f}+价<MA20/60"
                  f"{'+均线空头排列' if alignment < 0 else ''}")
    elif total <= -1:
        trend = "down"
        detail = (f"偏空共振(评分{total})：DIF<0+RSI{last_rsi:.0f}+价<MA20"
                  f"{'+死叉' if cross_score < 0 else ''}")
    else:
        trend = "neutral"
        detail = f"指标矛盾(评分{total})：方向不明确，多空拉锯"

    return trend, detail


# ════════════════════════ 指标眼主函数 ════════════════════════

def indicator_eye(daily_data: list[dict]) -> EyeVerdict:
    closes = [float(r["close"]) for r in daily_data]
    highs  = [float(r["high"]) for r in daily_data]
    lows   = [float(r["low"]) for r in daily_data]
    n = len(daily_data)

    mc = macd(closes)
    dif, dea, bar = mc["dif"], mc["dea"], mc["bar"]
    rsi14 = rsi(closes, 14)
    bb = bollinger(closes, 20, 2.0)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)

    # ── trend: 加权共振评分 ──
    trend, trend_detail = _indicator_trend_score(closes, dif, rsi14, ma20, ma60, n)

    # ── position: RSI极端 + 布林挤压 + 价格位置 ──
    last_rsi = _safe(rsi14, n - 1, 50)
    last_close = closes[-1] if n else 0

    bb_upper = _safe(bb["upper"], n - 1, 0)
    bb_lower = _safe(bb["lower"], n - 1, 0)
    bb_mid = _safe(bb["mid"], n - 1, 0)
    bb_width = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid > 0 else 5
    bb_pct_b = (last_close - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper > bb_lower else 50

    # 布林挤压百分位
    squeeze_info = _bollinger_squeeze_analysis(closes, n)
    is_squeeze = any("挤压" in s[1] or "收缩" in s[1] for s in squeeze_info)

    position_parts: list[str] = []
    if last_rsi > 75:
        position_parts.append(f"RSI={last_rsi:.0f}极度超买")
    elif last_rsi > 70:
        position_parts.append(f"RSI={last_rsi:.0f}超买区上沿")
    elif last_rsi < 25:
        position_parts.append(f"RSI={last_rsi:.0f}极度超卖")
    elif last_rsi < 30:
        position_parts.append(f"RSI={last_rsi:.0f}超卖区下沿")

    if is_squeeze:
        squeeze_detail = next((s[1] for s in squeeze_info if "挤压" in s[1] or "收缩" in s[1]), "")
        if squeeze_detail:
            position_parts.append(squeeze_detail)

    if bb_pct_b > 95:
        position_parts.append(f"价触布林上轨(%B={bb_pct_b:.0f})")
    elif bb_pct_b < 5:
        position_parts.append(f"价触布林下轨(%B={bb_pct_b:.0f})")

    if not position_parts:
        position_parts.append(f"RSI={last_rsi:.0f}正常，BB带宽{bb_width:.1f}%，%B={bb_pct_b:.0f}")

    # 位置判断
    if last_rsi > 70 or last_rsi < 30 or is_squeeze:
        position = "key_level"
    elif last_rsi > 60 or last_rsi < 40 or bb_pct_b > 80 or bb_pct_b < 20:
        position = "approaching"
    else:
        position = "mid_range"
    position_detail = "；".join(position_parts)

    # ── signal: 多层背离+钝化+挤压突破 ──
    all_signals: list[tuple[str, str, int, int]] = []  # (signal, detail, conf, priority)

    # Layer 1: MACD 背离 (最优先)
    macd_divs = _detect_macd_divergence(closes, dif, n)
    for sig, detail, conf in macd_divs:
        all_signals.append((sig, detail, conf, 1))

    # Layer 2: RSI Failure Swing (高优先)
    rsi_fs = _detect_rsi_failure_swing(rsi14, n)
    for sig, detail, conf in rsi_fs:
        all_signals.append((sig, detail, conf, 2))

    # Layer 3: RSI 背离
    rsi_divs = _detect_rsi_divergence_mtf(closes, rsi14, n)
    for sig, detail, conf in rsi_divs:
        all_signals.append((sig, detail, conf, 3))

    # Layer 4: 布林挤压突破
    for sig, detail, conf in squeeze_info:
        if "突破" in detail or "变盘" in detail:
            all_signals.append((sig, detail, conf, 3))
        elif "收缩" in detail:
            all_signals.append((sig, detail, conf, 5))

    # Layer 5: KDJ 钝化+金叉死叉
    kdj_sigs = _kdj_stagnation_analysis(highs, lows, closes, n)
    for sig, detail, conf in kdj_sigs:
        all_signals.append((sig, detail, conf, 4))

    # Layer 6: 经典超买超卖（最后的一道防线）
    if not all_signals:
        if last_rsi > 80:
            all_signals.append(("sell", f"RSI={last_rsi:.0f}极端超买，物极必反", 3, 5))
        elif last_rsi < 20:
            all_signals.append(("buy", f"RSI={last_rsi:.0f}极端超卖，低估修复在即", 3, 5))

    # 选择最优信号
    if all_signals:
        all_signals.sort(key=lambda x: (x[3], -x[2]))  # 优先级升序，置信度降序
        best = all_signals[0]
        signal, signal_detail, confidence = best[0], best[1], best[2]
        others = [s for s in all_signals if s[2] >= 4 and s != best]
        if others:
            signal_detail += f"（另见{'，'.join(s[1][:10] for s in others[:2])}）"
    else:
        signal, signal_detail = "none", "当前无指标异常信号"
        confidence = 1

    return EyeVerdict(
        eye="indicator", lens="数据·异常",
        trend=trend, trend_detail=trend_detail,
        position=position, position_detail=position_detail,
        signal=signal, signal_detail=signal_detail,
        confidence=confidence,
        horizon="mid",  # MACD背离/RSI背离需5-15日展开，KDJ钝化以周计
    )


# ════════════════════════ 3. 缠论眼 (级别·层次) ════════════════════════
#
# 深挖维度：
#   1. 走势类型 — 中枢序列方向+重叠度 → 上涨/下跌/盘整/扩张/收敛
#   2. 二买/二卖/类二买/类二卖 — 中枢回试确认跟进点
#   3. 多级别联立+区间套 — 大(全量)/中(60日)/小(20日)三级共振
#   4. 中枢引力 — 多中枢加权引力场，ZG支撑/ZD压力

def _chan_internals(daily_data: list[dict]):
    """层1-3：包含处理→分型→笔→中枢。返回完整中间状态（增强版含笔元数据）。"""
    n = len(daily_data)
    closes = [float(r["close"]) for r in daily_data]
    highs  = [float(r["high"]) for r in daily_data]
    lows   = [float(r["low"]) for r in daily_data]

    # 包含处理
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

    pivots: list[tuple[float, float]] = []
    pivot_strokes: list[list] = []
    for si in range(len(strokes) - 2):
        s0, s1, s2 = strokes[si], strokes[si + 1], strokes[si + 2]
        s_highs = [max(s[0][1], s[1][1]) for s in (s0, s1, s2)]
        s_lows = [min(s[0][1], s[1][1]) for s in (s0, s1, s2)]
        zg_ = min(s_highs)
        zd_ = max(s_lows)
        if zg_ > zd_:
            pivots.append((zg_, zd_))
            pivot_strokes.append([s0, s1, s2])

    stroke_details: list[dict] = []
    for s in strokes:
        start_pt, end_pt = s
        d = "down" if start_pt[2] == "top" else "up"
        bar_len = end_pt[0] - start_pt[0]
        price_range = abs(end_pt[1] - start_pt[1])
        velocity = price_range / bar_len if bar_len > 0 else 0
        stroke_details.append({
            "start_idx": start_pt[0], "end_idx": end_pt[0],
            "start_price": start_pt[1], "end_price": end_pt[1],
            "direction": d, "bars": bar_len,
            "range": price_range, "velocity": velocity,
        })

    return {
        "closes": closes, "highs": highs, "lows": lows,
        "tops": tops, "bottoms": bottoms, "tps": tps,
        "strokes": strokes, "stroke_details": stroke_details,
        "pivots": pivots, "pivot_strokes": pivot_strokes,
        "idx_map": idx_map,
    }


def _chan_trend_type_classify(pivots: list[tuple[float, float]],
                               stroke_details: list[dict],
                               closes: list[float], n: int) -> tuple[str, str, dict]:
    """走势类型：中枢序列方向+重叠度 → 上涨/下跌/盘整/扩张/收敛。

    上涨：ZG递增 + ZD递增 + 低重叠度 → 中枢逐级上移
    下跌：ZG递减 + ZD递减 + 低重叠度 → 中枢逐级下移
    盘整：高重叠度(>50%) → 中枢区间震荡
    扩张：ZG上移+ZD下移 → 波动加大酝酿趋势
    收敛：ZG下移+ZD上移 → 三角形整理即将变盘"""
    if len(pivots) < 2:
        return "neutral", "中枢不足(<2)，无法判定走势类型", {"type": "unknown"}

    recent = pivots[-3:]
    zg_vals = [p[0] for p in recent]
    zd_vals = [p[1] for p in recent]
    k = len(recent)

    zg_trend = _slope(zg_vals, k) if k >= 2 else 0
    zd_trend = _slope(zd_vals, k) if k >= 2 else 0

    overlaps: list[float] = []
    for i in range(len(recent) - 1):
        prev_zg, prev_zd = recent[i]
        cur_zg, cur_zd = recent[i + 1]
        denom = prev_zg - prev_zd
        if denom > 0 and cur_zd < prev_zg:
            overlaps.append((prev_zg - cur_zd) / denom * 100)
        else:
            overlaps.append(0)

    avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0

    up_count = sum(1 for s in stroke_details[-8:] if s["direction"] == "up") if stroke_details else 0
    down_count = len(stroke_details[-8:]) - up_count if stroke_details else 0

    last_close = closes[-1] if n > 0 else 0
    last_zg = zg_vals[-1]
    last_zd = zd_vals[-1]

    metrics = {
        "zg_vals": zg_vals, "zd_vals": zd_vals,
        "zg_trend": round(zg_trend, 4), "zd_trend": round(zd_trend, 4),
        "avg_overlap": round(avg_overlap, 1),
        "up_strokes": up_count, "down_strokes": down_count,
    }

    if zg_trend > 0 and zd_trend > 0:
        if avg_overlap < 30:
            if last_close > last_zg:
                return "up", f"上涨趋势加速：中枢逐级上移(ZG{zg_vals[0]:.2f}→{zg_vals[-1]:.2f})，价格突破ZG{zg_vals[-1]:.2f}", metrics
            return "up", f"上涨趋势：中枢逐级上移(ZG{zg_vals[0]:.2f}→{zg_vals[-1]:.2f})，多头结构完整", metrics
        if zg_trend > 0.3 and zd_trend > 0.3:
            return "up", f"上涨构建中：ZG+ZD双升(斜率{zg_trend:+.3f}/{zd_trend:+.3f})，中枢箱体上移", metrics

    if zg_trend < 0 and zd_trend < 0:
        if avg_overlap < 30:
            if last_close < last_zd:
                return "down", f"下跌趋势加速：中枢逐级下移(ZD{zd_vals[0]:.2f}→{zd_vals[-1]:.2f})，价格跌破ZD{zd_vals[-1]:.2f}", metrics
            return "down", f"下跌趋势：中枢逐级下移(ZD{zd_vals[0]:.2f}→{zd_vals[-1]:.2f})，空头结构完整", metrics
        if zg_trend < -0.3 and zd_trend < -0.3:
            return "down", f"下跌构建中：ZG+ZD双降(斜率{zg_trend:+.3f}/{zd_trend:+.3f})，中枢箱体下移", metrics

    if avg_overlap > 50:
        if last_close > last_zg:
            return "neutral", f"中枢震荡偏多：重叠{avg_overlap:.0f}%，价格在ZG{last_zg:.2f}上方试探突破", metrics
        elif last_close < last_zd:
            return "neutral", f"中枢震荡偏空：重叠{avg_overlap:.0f}%，价格在ZD{last_zd:.2f}下方试探破位", metrics
        return "neutral", f"中枢震荡：重叠{avg_overlap:.0f}%，ZG{last_zg:.2f}-ZD{last_zd:.2f}区间整理", metrics

    if zg_trend > 0 and zd_trend < 0:
        return "neutral", f"中枢扩张：ZG上移+ZD下移，波动加大，趋势酝酿中", metrics

    if zg_trend < 0 and zd_trend > 0:
        return "neutral", f"中枢收敛：ZG下移+ZD上移→三角形整理，即将变盘", metrics

    return "neutral", f"走势结构模糊 ZG趋势{zg_trend:+.3f}/ZD趋势{zd_trend:+.3f}", metrics


def _chan_pivot_gravity(pivots: list[tuple[float, float]], last_close: float) -> tuple[str, str]:
    """中枢引力：多中枢加权引力场（近中枢权0.5→次近0.3→再次0.2）。

    ZG对下方价格有吸引，对上方价格有支撑；ZD对上方价格有吸引，对下方价格有压力。"""
    if not pivots:
        return "no_pivot", "无中枢参考"

    weights = [0.5, 0.3, 0.2]
    relevant = pivots[-3:]

    zg_pull = 0.0
    zd_pull = 0.0
    details: list[str] = []

    for i, (zg, zd) in enumerate(relevant):
        w = weights[min(i, len(weights) - 1)]
        dist_zg = (last_close - zg) / zg * 100
        dist_zd = (last_close - zd) / zd * 100
        strength_zg = max(0, 1 - abs(dist_zg) / 10)
        strength_zd = max(0, 1 - abs(dist_zd) / 10) * 0.5

        if dist_zg > 0:
            zg_pull += w * strength_zg
            if abs(dist_zg) < 2:
                details.append(f"ZG{zg:.2f}支撑(距{dist_zg:+.1f}%)")
        else:
            zg_pull -= w * strength_zg
            if abs(dist_zg) < 2:
                details.append(f"ZG{zg:.2f}压力(距{dist_zg:+.1f}%)")

        if dist_zd > 0:
            zd_pull += w * strength_zd
            if abs(dist_zd) < 2:
                details.append(f"ZD{zd:.2f}支撑(距{dist_zd:+.1f}%)")
        else:
            zd_pull -= w * strength_zd
            if abs(dist_zd) < 2:
                details.append(f"ZD{zd:.2f}压力(距{dist_zd:+.1f}%)")

    net_pull = zg_pull + zd_pull

    if net_pull > 0.3:
        return "upward_gravity", f"中枢引力偏多(净{net_pull:+.2f})" + ("：" + "；".join(details[:2]) if details else "")
    elif net_pull < -0.3:
        return "downward_gravity", f"中枢引力偏空(净{net_pull:+.2f})" + ("：" + "；".join(details[:2]) if details else "")
    else:
        return "neutral_gravity", f"中枢引力平衡(净{net_pull:+.2f})" + ("：" + "；".join(details[:2]) if details else "")


def _chan_second_bs(internals: dict, closes: list[float], n: int) -> list[tuple[str, str, int]]:
    """二买/二卖/类二买/类二卖 检测。

    二买：一买后回调不破前低+在ZD附近 → 中枢回试确认跟进
    二卖：一卖后反弹不过前高+在ZG附近 → 中枢回试确认减仓
    类二买/类二卖：中枢内第二次试ZD/ZG，整固中"""
    results: list[tuple[str, str, int]] = []
    pivots = internals["pivots"]
    bottoms = internals["bottoms"]
    tops = internals["tops"]

    if len(pivots) < 1:
        return results

    last_zg = pivots[-1][0]
    last_zd = pivots[-1][1]

    recent_bots = sorted(bottoms, key=lambda x: x[0])[-4:]
    recent_tops = sorted(tops, key=lambda x: x[0])[-4:]

    if len(recent_bots) >= 2 and last_zd > 0:
        b1_price = recent_bots[-2][1]
        b2_price = recent_bots[-1][1]
        b1_idx = recent_bots[-2][0]
        b2_idx = recent_bots[-1][0]
        if b2_price > b1_price:
            dist = abs(b2_price - last_zd) / last_zd * 100
            if dist < 3:
                results.append(("buy",
                    f"二买：前底{b1_price:.2f}→现底{b2_price:.2f}(抬高)，ZD{last_zd:.2f}支撑确认，跟进点", 4))
            elif dist < 5 and b2_idx - b1_idx >= 3:
                results.append(("buy",
                    f"类二买：中枢内二次回试，底{b2_price:.2f}>前底{b1_price:.2f}，结构整固中", 3))

    if len(recent_tops) >= 2 and last_zg > 0:
        t1_price = recent_tops[-2][1]
        t2_price = recent_tops[-1][1]
        t1_idx = recent_tops[-2][0]
        t2_idx = recent_tops[-1][0]
        if t2_price < t1_price:
            dist = abs(t2_price - last_zg) / last_zg * 100
            if dist < 3:
                results.append(("sell",
                    f"二卖：前顶{t1_price:.2f}→现顶{t2_price:.2f}(降低)，ZG{last_zg:.2f}压力确认，减仓点", 4))
            elif dist < 5 and t2_idx - t1_idx >= 3:
                results.append(("sell",
                    f"类二卖：中枢内二次回试，顶{t2_price:.2f}<前顶{t1_price:.2f}，结构转弱", 3))

    if last_zd > 0 and len(pivots) >= 2:
        zd_touches = sum(1 for b in bottoms if abs(b[1] - last_zd) / last_zd < 0.015)
        if zd_touches >= 5:
            results.append(("buy",
                f"中枢ZD{last_zd:.2f}多次触及({zd_touches}次)→强力支撑带", 3))

    if last_zg > 0 and len(pivots) >= 2:
        zg_touches = sum(1 for t in tops if abs(t[1] - last_zg) / last_zg < 0.015)
        if zg_touches >= 5:
            results.append(("sell",
                f"中枢ZG{last_zg:.2f}多次触及({zg_touches}次)→强力压力带", 3))

    return results


def _chan_multi_level_analysis(daily_data: list[dict]) -> dict:
    """多级别联立 + 区间套：大(全量≈周线)/中(60日≈日线)/小(20日≈60分)三级。

    日线数据用窗口模拟多级别：
    - 大级别定方向 → 中级别定结构 → 小级别定买卖点
    - 区间套：小级别信号+中级别结构+大级别方向三重确认"""
    n = len(daily_data)

    levels = {}
    windows = {"large": min(n, max(120, n)), "mid": min(n, 60), "small": min(n, 20)}

    for lv, w in windows.items():
        if w < 20:
            levels[lv] = None
            continue
        segment = daily_data[-w:]
        try:
            internals = _chan_internals(segment)
            internals["window"] = w
            levels[lv] = internals
        except Exception:
            levels[lv] = None

    large = levels.get("large")
    mid = levels.get("mid")
    small = levels.get("small")

    large_trend = "neutral"
    if large and large["pivots"]:
        lg_zg = large["pivots"][-1][0]
        lg_zd = large["pivots"][-1][1]
        lg_last = large["closes"][-1] if large["closes"] else 0
        if lg_last > lg_zg:
            large_trend = "up"
        elif lg_last < lg_zd:
            large_trend = "down"

    mid_phase = "unknown"
    mid_zg = 0.0
    mid_zd = 0.0
    if mid and mid["pivots"]:
        mid_zg = mid["pivots"][-1][0]
        mid_zd = mid["pivots"][-1][1]
        mid_last = mid["closes"][-1] if mid["closes"] else 0
        if mid_last > mid_zg:
            mid_phase = "above_zg"
        elif mid_last < mid_zd:
            mid_phase = "below_zd"
        else:
            mid_phase = "in_pivot"

    small_signal = "none"
    small_signal_detail = ""
    if small and small["bottoms"] and small["tops"]:
        small_closes = small["closes"]
        small_bot = small["bottoms"][-1] if small["bottoms"] else None
        small_top = small["tops"][-1] if small["tops"] else None
        if len(small_closes) >= 26:
            small_bar = macd(small_closes)["bar"]
            small_ds = [(s[0][0], s[1][0]) for s in small["strokes"] if s[0][2] == "top" and s[1][2] == "bottom"]
            small_us = [(s[0][0], s[1][0]) for s in small["strokes"] if s[0][2] == "bottom" and s[1][2] == "top"]

            def _seg_area(a: int, b: int, bar: list) -> float:
                return sum(abs(bar[k]) for k in range(a, min(b + 1, len(bar))) if bar[k] is not None)

            if small_bot and len(small_ds) >= 2:
                ca = _seg_area(small_ds[-1][0], small_ds[-1][1], small_bar)
                pa = _seg_area(small_ds[-2][0], small_ds[-2][1], small_bar)
                if pa > 0 and ca < pa * 0.85:
                    small_signal = "buy"
                    small_signal_detail = f"小级别一买(底分型{small_bot[1]:.2f}+MACD背驰)"

            if small_signal == "none" and small_top and len(small_us) >= 2:
                ca = _seg_area(small_us[-1][0], small_us[-1][1], small_bar)
                pa = _seg_area(small_us[-2][0], small_us[-2][1], small_bar)
                if pa > 0 and ca < pa * 0.85:
                    small_signal = "sell"
                    small_signal_detail = f"小级别一卖(顶分型{small_top[1]:.2f}+MACD顶背驰)"

    resonance_score = 0
    resonance_parts: list[str] = []

    if large_trend == "up":
        resonance_score += 1
        resonance_parts.append("大级别多头")
        if mid_phase in ("above_zg", "in_pivot"):
            resonance_score += 2
            resonance_parts.append("中级别结构偏多")
        if small_signal == "buy":
            resonance_score += 3
            resonance_parts.append("小级别买点共振→区间套定位")
    elif large_trend == "down":
        resonance_score -= 1
        resonance_parts.append("大级别空头")
        if mid_phase in ("below_zd",):
            resonance_score -= 2
            resonance_parts.append("中级别结构偏空")
        if small_signal == "sell":
            resonance_score -= 3
            resonance_parts.append("小级别卖点共振→区间套定位")
    elif large_trend == "neutral":
        if mid_phase == "above_zg":
            resonance_score += 2
            resonance_parts.append("中级别突破中枢上沿")
        elif mid_phase == "below_zd":
            resonance_score -= 2
            resonance_parts.append("中级别跌破中枢下沿")

    if mid_phase == "above_zg" and small_signal == "sell":
        resonance_parts.append("警示：中多+小空→回调非反转")
    elif mid_phase == "below_zd" and small_signal == "buy":
        resonance_parts.append("警示：中空+小多→反弹非反转")

    return {
        "levels": {k: ("ok" if v else "insufficient") for k, v in levels.items()},
        "large_trend": large_trend,
        "mid_phase": mid_phase,
        "mid_zg": mid_zg,
        "mid_zd": mid_zd,
        "small_signal": small_signal,
        "small_signal_detail": small_signal_detail,
        "resonance_score": resonance_score,
        "resonance_detail": " + ".join(resonance_parts) if resonance_parts else "无多级共振",
    }


def chan_eye(daily_data: list[dict]) -> EyeVerdict:
    n = len(daily_data)
    if n < 20:
        return EyeVerdict(
            eye="chan", lens="级别·层次",
            trend="neutral", trend_detail="数据不足(<20日)",
            position="mid_range", position_detail="",
            signal="none", signal_detail="", confidence=1,
            horizon="long",  # 中枢结构变化需10-30日跨级别确认
        )

    internals = _chan_internals(daily_data)
    tops = internals["tops"]
    bottoms = internals["bottoms"]
    strokes = internals["strokes"]
    pivots = internals["pivots"]
    stroke_details = internals["stroke_details"]
    closes = internals["closes"]

    zg = pivots[-1][0] if pivots else 0.0
    zd = pivots[-1][1] if pivots else 0.0
    last_close = closes[-1]

    # ── 1. 走势类型分类 ──
    trend_type, trend_type_detail, trend_metrics = _chan_trend_type_classify(
        pivots, stroke_details, closes, n)

    # ── 2. 多级别联立 + 区间套 ──
    multi_level = _chan_multi_level_analysis(daily_data)
    resonance_score = multi_level["resonance_score"]
    resonance_detail = multi_level["resonance_detail"]

    # ── 3. 趋势：多级共振优先于走势类型（后者看全量数据可能滞后） ──
    # 走势类型提供结构背景，多级别联立提供方向信号
    trend_from_ml = "up" if resonance_score >= 2 else ("down" if resonance_score <= -2 else "neutral")

    if trend_type == trend_from_ml and trend_from_ml != "neutral":
        trend, trend_detail = trend_from_ml, f"{'上涨' if trend_from_ml=='up' else '下跌'}趋势+多级共振(评分{resonance_score})：{trend_type_detail}；{resonance_detail}"
    elif trend_from_ml != "neutral":
        ml_label = "多级多头共振" if trend_from_ml == "up" else "多级空头共振"
        trend, trend_detail = trend_from_ml, f"{ml_label}(评分{resonance_score})替代走势类型({trend_type})判断：{resonance_detail}"
    elif trend_type in ("up", "down"):
        trend, trend_detail = trend_type, trend_type_detail
    elif zg > 0 and last_close > zg:
        trend, trend_detail = "up", f"价格突破ZG{zg:.2f}偏多（{trend_type_detail}）"
    elif zd > 0 and last_close < zd:
        trend, trend_detail = "down", f"价格跌破ZD{zd:.2f}偏空（{trend_type_detail}）"
    else:
        trend, trend_detail = "neutral", trend_type_detail

    # ── 4. 位置：中枢引力 + ZG/ZD距离 ──
    gravity_type, gravity_detail = _chan_pivot_gravity(pivots, last_close)

    pos_parts: list[str] = []
    if zg > 0:
        d_zg = abs(last_close - zg) / zg * 100
        if d_zg < 2:
            pos_parts.append(f"紧贴ZG{zg:.2f}({d_zg:.1f}%)")
        elif d_zg < 5:
            pos_parts.append(f"接近ZG{zg:.2f}({d_zg:.1f}%)")
    if zd > 0 and zg > 0:
        d_zd = abs(last_close - zd) / zd * 100
        if d_zd < 2:
            pos_parts.append(f"紧贴ZD{zd:.2f}({d_zd:.1f}%)")
        elif d_zd < 5:
            pos_parts.append(f"接近ZD{zd:.2f}({d_zd:.1f}%)")
        pivot_range = zg - zd
        if pivot_range > 0:
            pip = (last_close - zd) / pivot_range * 100
            if 0 <= pip <= 100:
                pos_parts.append(f"中枢内{pip:.0f}%位")

    if not pos_parts:
        pos_parts.append("无明显关键位")

    position_detail = "；".join(pos_parts) + " | " + gravity_detail

    if gravity_type == "upward_gravity" and trend == "up":
        position = "key_level"
    elif gravity_type == "downward_gravity" and trend == "down":
        position = "key_level"
    elif zg > 0 and (abs(last_close - zg) / zg * 100 < 2 or
                     (zd > 0 and abs(last_close - zd) / zd * 100 < 2)):
        position = "key_level"
    elif zg > 0 and abs(last_close - zg) / zg * 100 < 5:
        position = "approaching"
    else:
        position = "mid_range"

    # ── 5. 信号：一买/一卖/二买/二卖/三买/区间套 ──
    mc = macd(closes)
    macd_bar = mc["bar"]

    def _bar_area(a: int, b: int) -> float:
        total = 0.0
        for k in range(a, min(b + 1, len(macd_bar))):
            if macd_bar[k] is not None:
                total += abs(macd_bar[k])
        return total

    down_strokes = [(s[0][0], s[1][0]) for s in strokes if s[0][2] == "top" and s[1][2] == "bottom"]
    up_strokes = [(s[0][0], s[1][0]) for s in strokes if s[0][2] == "bottom" and s[1][2] == "top"]

    last_bot = bottoms[-1] if bottoms else None
    last_top = tops[-1] if tops else None

    sigs: list[tuple[str, str, int]] = []

    # 一买：底分型 + MACD柱面积背驰
    if last_bot and len(down_strokes) >= 2:
        for di in range(len(down_strokes) - 1, 0, -1):
            if down_strokes[di][1] == last_bot[0]:
                cur_a = _bar_area(down_strokes[di][0], down_strokes[di][1])
                prev_a = _bar_area(down_strokes[di - 1][0], down_strokes[di - 1][1])
                if prev_a > 0 and cur_a < prev_a * 0.85:
                    sigs.append(("buy",
                        f"一买：底分型{last_bot[1]:.2f}+MACD背驰(面积{cur_a:.0f}<前{prev_a:.0f})，空方力竭", 5))
                    break

    # 三买：回调不破ZG
    if last_bot and zg > 0 and last_bot[1] > zg:
        if any(s[1][0] == last_bot[0] and s[0][2] == "top" for s in strokes):
            sigs.append(("buy",
                f"三买：回调不破ZG{zg:.2f}，中枢支撑有效", 5))

    # 一卖：顶分型 + MACD顶背驰
    if last_top and len(up_strokes) >= 2:
        for ui in range(len(up_strokes) - 1, 0, -1):
            if up_strokes[ui][1] == last_top[0]:
                cur_a = _bar_area(up_strokes[ui][0], up_strokes[ui][1])
                prev_a = _bar_area(up_strokes[ui - 1][0], up_strokes[ui - 1][1])
                if prev_a > 0 and cur_a < prev_a * 0.85:
                    sigs.append(("sell",
                        f"一卖：顶分型{last_top[1]:.2f}+MACD顶背驰，多方力竭", 5))
                    break

    # 二买/二卖/类二买/类二卖
    sigs.extend(_chan_second_bs(internals, closes, n))

    # 区间套
    if multi_level["small_signal"] == "buy" and resonance_score >= 3:
        sigs.append(("buy",
            f"区间套买点：{multi_level['small_signal_detail']}+大中级别共振确认", 5))
    elif multi_level["small_signal"] == "sell" and resonance_score <= -3:
        sigs.append(("sell",
            f"区间套卖点：{multi_level['small_signal_detail']}+大中级别共振确认", 5))

    priority = {"一买": 1, "一卖": 1, "区间套买点": 2, "区间套卖点": 2,
                "二买": 3, "二卖": 3, "三买": 4, "三卖": 4, "类二买": 5, "类二卖": 5}

    def _sig_rank(s: tuple) -> int:
        for kw, r in priority.items():
            if kw in s[1]:
                return r
        return 99

    sigs.sort(key=_sig_rank)

    if sigs:
        signal, signal_detail, confidence = sigs[0]
    else:
        signal, signal_detail, confidence = "none", "", 3

    if len(pivots) >= 3:
        confidence = min(5, confidence + 1)

    return EyeVerdict(
        eye="chan", lens="级别·层次",
        trend=trend, trend_detail=trend_detail,
        position=position, position_detail=position_detail,
        signal=signal, signal_detail=signal_detail,
        confidence=confidence,
        horizon="long",  # 中枢结构变化需10-30日跨级别确认
    )


# ════════════════════════ 4. 波浪眼 (形态·结构) ════════════════════════
#
# 深挖维度：
#   1. 斐波那契比率验证 — 0.382/0.5/0.618/0.786 回调位+1.272/1.618扩展位
#   2. 交替原则 — 2浪简单→4浪复杂（平台/三角），2浪复杂→4浪简单（锯齿）
#   3. 通道约束 — 连接1-3浪延伸线约束4浪底+5浪顶
#   4. 第1浪确认+5浪衰竭 — 动能分歧+量价背离
#   5. Elliott计数引擎 — 规则导向的波浪标注

def _wave_fib_retrace_levels(a: float, b: float):
    """计算 A→B 的斐波那契回调/扩展位。"""
    diff = b - a
    return {
        "0.236": a + diff * 0.236,
        "0.382": a + diff * 0.382,
        "0.500": a + diff * 0.500,
        "0.618": a + diff * 0.618,
        "0.786": a + diff * 0.786,
        "1.000": b,
        "1.272": a + diff * 1.272,
        "1.618": a + diff * 1.618,
    }


def _wave_fib_cluster(zones: list[dict], threshold: float = 0.02) -> list[dict]:
    """多浪段斐波那契位聚簇——两个浪段的斐波位在 threshold 内重合视为共振区。"""
    clusters: list[dict] = []
    all_pts: list[tuple[float, str, int]] = []
    for zi, zd in enumerate(zones):
        for label, price in zd.items():
            all_pts.append((price, label, zi))
    all_pts.sort(key=lambda x: x[0])
    done = [False] * len(all_pts)
    for i in range(len(all_pts)):
        if done[i]:
            continue
        group = [all_pts[i]]
        for j in range(i + 1, len(all_pts)):
            if done[j]:
                continue
            if abs(all_pts[j][0] - all_pts[i][0]) / max(abs(all_pts[i][0]), 1.0) < threshold:
                group.append(all_pts[j])
                done[j] = True
        done[i] = True
        if len(group) >= 2:
            labels = [f"{g[1]}(段{g[2]})" for g in group]
            clusters.append({
                "price": round(sum(g[0] for g in group) / len(group), 2),
                "count": len(group),
                "labels": labels,
            })
    clusters.sort(key=lambda x: -x["count"])
    return clusters


def _wave_alternation_principle(strokes: list[dict], sh: list[tuple], sl: list[tuple]) -> str:
    """交替原则：2浪(简单)和4浪(复杂)形态不同。
    - 急跌急涨=简单（锯齿），横盘震荡=复杂（平台/三角）
    - 2浪简单 → 4浪大概率复杂，反之亦然"""
    if len(strokes) < 5:
        return "波数不足(<5)，交替原则暂不适用"

    def _wave_style(wave_strokes: list[dict]) -> str:
        if not wave_strokes:
            return "unknown"
        durations = [s["bars"] for s in wave_strokes]
        ranges = [s["range"] for s in wave_strokes]
        avg_dur = sum(durations) / len(durations) if durations else 0
        avg_range = sum(ranges) / len(ranges) if ranges else 0
        avg_velocity = sum(ranges[i] / durations[i] for i in range(len(durations)) if durations[i] > 0) / len(durations)
        # 简单=速度快/bar少 → 锯齿形
        if avg_dur < 8 and avg_velocity > 0.02:
            return "simple_zigzag"
        return "complex_flat"

    # 2浪: 第1上升笔之后的第一段下跌
    wave1 = strokes[0] if len(strokes) >= 1 else None
    wave2_strokes: list[dict] = []
    wave4_strokes: list[dict] = []
    in_w2 = False
    w2_end = 0
    if wave1 and wave1["direction"] == "up":
        for s in strokes[1:]:
            if s["direction"] == "down" and not in_w2:
                in_w2 = True
                wave2_strokes.append(s)
                w2_end = s["end_idx"]
            elif in_w2 and s["direction"] == "down":
                wave2_strokes.append(s)
                w2_end = s["end_idx"]
            elif in_w2 and s["direction"] == "up":
                break

    w4_start = w2_end if w2_end > 0 else 0
    for s in strokes:
        if s["start_idx"] >= w4_start and s["direction"] == "down":
            wave4_strokes.append(s)

    w2_style = _wave_style(wave2_strokes) if wave2_strokes else "unknown"
    w4_style = _wave_style(wave4_strokes) if wave4_strokes else "unknown"

    if w2_style == "simple_zigzag" and w4_style == "complex_flat":
        return "2浪急跌(简单锯齿)→4浪横盘(复杂平台)，交替有效，4浪即将结束"
    if w2_style == "complex_flat" and w4_style == "simple_zigzag":
        return "2浪横盘(复杂)→4浪急跌(简单锯齿)，交替有效"
    if w2_style == w4_style and w2_style != "unknown":
        return f"2浪和4浪同为{w2_style.replace('_',' ')}，交替异常，需重新审视波浪计数"
    return "交替信息不足"


def _wave_channel_constraint(highs: list[float], lows: list[float],
                              sh: list[tuple], sl: list[tuple]) -> tuple[str, str]:
    """通道约束：连接波峰1-3画上轨，平行下轨通过波谷2。
    价格突破通道上轨=5浪延长，跌破下轨=调整级别扩大。
    返回通道状态+价格位置。"""
    n = len(highs)
    if len(sh) < 2 or len(sl) < 2:
        return "no_channel", "波峰波谷不足，无法画通道"

    sh_sorted = sorted(sh, key=lambda x: x[0])
    sl_sorted = sorted(sl, key=lambda x: x[0])

    # 1浪顶: 第一个显著波峰, 2浪底: 其后第一个显著波谷
    peak1 = sh_sorted[0]
    trough2 = sl_sorted[0] if sl_sorted else peak1
    peak3_idx = -1
    for hp in sh_sorted[1:]:
        if hp[0] > trough2[0] and hp[1] > peak1[1]:
            peak3_idx = sh_sorted.index(hp) if hp in sh_sorted else -1
            break
    if peak3_idx < 0 and len(sh_sorted) >= 3:
        possible = [p for p in sh_sorted if p[0] > trough2[0] and p[1] > peak1[1]]
        if possible:
            peak3 = max(possible, key=lambda x: x[1])
        else:
            peak3 = sorted(sh_sorted, key=lambda x: -x[1])[0]
    else:
        peak3 = sh_sorted[-1] if len(sh_sorted) >= 2 else None
        if peak3 and peak3[0] <= trough2[0]:
            peak3 = None
            for hp in sh_sorted[1:]:
                if hp[0] > trough2[0] and hp[1] > peak1[1]:
                    peak3 = hp
                    break
    if peak3 is None:
        peak3 = max(sh_sorted, key=lambda x: x[1])

    upper_slope = (peak3[1] - peak1[1]) / (peak3[0] - peak1[0]) if peak3[0] != peak1[0] else 0
    lower_origin = trough2[1]

    last_close = highs[-1] if n > 0 else 0
    target_upper = peak3[1] + upper_slope * (n - 1 - peak3[0])
    target_lower = lower_origin + upper_slope * (n - 1 - trough2[0])

    last_idx = n - 1
    upper_now = peak1[1] + upper_slope * (last_idx - peak1[0])
    lower_now = lower_origin + upper_slope * (last_idx - trough2[0])

    if upper_now <= lower_now:
        return "no_channel", "通道斜率异常，上下轨交叉"

    pos_pct = (last_close - lower_now) / (upper_now - lower_now) * 100

    if pos_pct > 100:
        return "above_channel", f"突破通道上轨{target_upper:.2f}(价格{last_close:.2f})，5浪延长中"
    elif pos_pct > 80:
        return "upper_channel", f"接近通道上轨{target_upper:.2f}({pos_pct:.0f}%)，5浪目标位"
    elif pos_pct < 0:
        return "below_channel", f"跌破通道下轨{target_lower:.2f}，调整级别扩大"
    elif pos_pct < 20:
        return "lower_channel", f"通道下轨附近{target_lower:.2f}({pos_pct:.0f}%)，4浪底区域"
    else:
        return "mid_channel", f"通道中段({pos_pct:.0f}%)，波浪推进中"


def _wave_elliott_count(sh: list[tuple], sl: list[tuple],
                         closes: list[float]) -> tuple[str, str, int]:
    """Elliott 5+3计数引擎：规则导向标注波浪阶段。
    5浪推动 → 3浪调整 → 新的5浪。返回当前阶段+置信度。"""
    if len(sh) < 3 or len(sl) < 3:
        return "unknown", "波峰波谷不足(需≥3+3)，无法标注", 0

    sh_sorted = sorted(sh, key=lambda x: x[0])
    sl_sorted = sorted(sl, key=lambda x: x[0])

    # 合并为时间序列
    turning_points: list[tuple[int, float, str]] = []
    for idx, val in sh_sorted:
        turning_points.append((idx, val, "high"))
    for idx, val in sl_sorted:
        turning_points.append((idx, val, "low"))
    turning_points.sort(key=lambda x: x[0])

    # 检查推动波结构: high→low→high→low→high 完成5浪
    # 推动波要求交替：高-低-高-低-高 (5浪上升) 或 低-高-低-高-低 (5浪下跌)
    tp_recent = turning_points[-7:]
    if len(tp_recent) < 5:
        return "unknown", f"转折点不足(近段{turning_points[-1][0]}仅{len(tp_recent)}个)", 0

    # 判断 bull cycle: (high,low,high,low,high) → 上升5浪
    # 判断 bear cycle: (low,high,low,high,low) → 下跌5浪
    is_bull = tp_recent[0][2] == "high"
    impulse_highs: list[tuple] = []
    impulse_lows: list[tuple] = []

    if is_bull:
        for i in range(0, len(tp_recent) - 1, 2):
            if i < len(tp_recent) and tp_recent[i][2] == "high":
                impulse_highs.append(tp_recent[i])
            if i + 1 < len(tp_recent) and tp_recent[i + 1][2] == "low":
                impulse_lows.append(tp_recent[i + 1])
    else:
        for i in range(0, len(tp_recent) - 1, 2):
            if i < len(tp_recent) and tp_recent[i][2] == "low":
                impulse_lows.append(tp_recent[i])
            if i + 1 < len(tp_recent) and tp_recent[i + 1][2] == "high":
                impulse_highs.append(tp_recent[i + 1])

    # 推进是否衰竭?
    impulse_complete = False
    confidence = 2

    if len(impulse_highs) >= 3:
        h1, h3, h5 = impulse_highs[0][1], impulse_highs[1][1], impulse_highs[2][1]
        if h5 < h3 and h5 < h1:
            impulse_complete = True
            confidence = 4
        elif h5 < h3:
            impulse_complete = True
            confidence = 3

    if len(impulse_lows) >= 2:
        for i in range(1, len(impulse_lows)):
            if impulse_lows[i][1] < impulse_lows[i - 1][1]:
                impulse_complete = True
                break

    # 确定当前阶段 — 使用推动高点的价格进展而非简单计数
    n_h = len(impulse_highs)
    n_l = len(impulse_lows)

    # 价格进展用于确认推动方向
    if n_h >= 2 and n_l >= 2:
        h_progress = impulse_highs[-1][1] > impulse_highs[0][1]
        l_progress = impulse_lows[-1][1] > impulse_lows[0][1]

        if impulse_complete:
            stage, detail = f"5浪+调整", f"推动5浪已走完({n_h}高+{n_l}低)，进入ABC调整段"
            confidence = max(3, confidence)
        elif n_h >= 3 and n_l >= 2 and h_progress and l_progress:
            # rising HH + HL with 3+ impulse highs → 5-wave push
            stage, detail = "5浪末端", f"第5浪加速(第{n_h}高点)，注意衰竭"
            confidence = 3
        elif n_h >= 2 and n_l >= 2 and h_progress and l_progress:
            stage, detail = "3浪主升", f"第3浪进行中({n_h}高+{n_l}低)，最强推动段"
        elif n_h >= 1 and n_l >= 1 and h_progress:
            stage, detail = "2浪回调", f"1浪顶已见({impulse_highs[-1][1]:.2f})，回调中，等3浪信号"
        else:
            stage, detail = "整理中", f"转折点排列非标准推动结构"
    elif n_h >= 1 and n_l >= 1:
        stage, detail = "1浪启动", "第1浪初期，结构尚不完整"
    else:
        stage, detail = "整理中", "趋势不明，非标准波浪结构"

    return stage, detail, confidence


def wave_eye(daily_data: list[dict]) -> EyeVerdict:
    closes = [float(r["close"]) for r in daily_data]
    highs  = [float(r["high"]) for r in daily_data]
    lows   = [float(r["low"]) for r in daily_data]
    n = len(daily_data)

    if n < 30:
        return EyeVerdict(
            eye="wave", lens="形态·结构",
            trend="neutral", trend_detail="数据不足(<30日)",
            position="mid_range", position_detail="",
            signal="none", signal_detail="", confidence=1,
            horizon="long",  # 浪型完成需10-40日，3浪/5浪不会瞬间展开
        )

    sh, sl = _swing_points(highs, lows, lookback=7)

    sh_vals = [v for _, v in sh]
    sl_vals = [v for _, v in sl]

    sh_sorted = sorted(sh, key=lambda x: x[0])
    sl_sorted = sorted(sl, key=lambda x: x[0])

    # ── 1. Elliott计数 ──
    wave_stage, wave_detail, count_conf = _wave_elliott_count(sh, sl, closes)

    # ── 2. 趋势：均线坡度做主裁决，波浪结构做副证 ──
    ma20 = sma(closes, 20)
    last_ma20 = _safe(ma20, n - 1, closes[-1])
    prev_ma20 = _safe(ma20, n - 21, last_ma20) if n >= 21 else last_ma20
    ma_slope = (last_ma20 - prev_ma20) / prev_ma20 * 100 if prev_ma20 > 0 else 0

    # HH/HL 模式
    hh = len(sh_vals) >= 2 and sh_vals[-1] > sh_vals[-2]
    hl = len(sl_vals) >= 2 and sl_vals[-1] > sl_vals[-2]
    lh = len(sh_vals) >= 2 and sh_vals[-1] < sh_vals[-2]
    ll = len(sl_vals) >= 2 and sl_vals[-1] < sl_vals[-2]

    if hh and hl and ma_slope > 0:
        trend, trend_detail = "up", f"HH+HL+MA20上行({wave_stage})，趋势向上"
    elif ll and lh and ma_slope < 0:
        trend, trend_detail = "down", f"LL+LH+MA20下行({wave_stage})，趋势向下"
    elif ma_slope > 1.0:
        trend, trend_detail = "up", f"MA20强势上行({wave_stage})，波浪结构跟随"
    elif ma_slope < -1.0:
        trend, trend_detail = "down", f"MA20强势下行({wave_stage})，波浪结构跟随"
    elif hh and hl:
        trend, trend_detail = "up", f"HH+HL推动({wave_stage})，趋势向上"
    elif ll and lh:
        trend, trend_detail = "down", f"LL+LH推动({wave_stage})，趋势向下"
    elif "5浪+调整" in wave_stage:
        trend, trend_detail = "neutral", f"5浪完成({wave_stage})，方向待选择"
    else:
        trend, trend_detail = "neutral", f"转折结构不明({wave_stage})"

    # ── 3. 斐波那契验证 ──
    fib_zones: list[dict] = []
    if len(sh) >= 2 and len(sl) >= 2:

        # 浪1: 第一个上升段 low→high
        if sl_sorted[0][0] < sh_sorted[0][0]:
            w1_low = sl_sorted[0][1]
            w1_high = sh_sorted[0][1] if sh_sorted[0][0] > sl_sorted[0][0] else next((s[1] for s in sh_sorted if s[0] > sl_sorted[0][0]), sh_sorted[0][1])
        else:
            w1_low = sl_sorted[0][1]
            w1_high = sh_sorted[0][1] if len(sh_sorted) > 1 else sh_sorted[0][1]

        if w1_low < w1_high:
            fib_zones.append(_wave_fib_retrace_levels(w1_low, w1_high))

        # 浪1-3段: 启动→浪3高点
        if len(sh_sorted) >= 2 and len(sl_sorted) >= 2:
            wave_start_low = min(wl[1] for wl in sl_sorted[:2])
            wave3_high = max(s[1] for s in sh_sorted[:3]) if len(sh_sorted) >= 2 else w1_high
            if wave_start_low < wave3_high:
                fib_zones.append(_wave_fib_retrace_levels(wave_start_low, wave3_high))

    fib_clusters = _wave_fib_cluster(fib_zones, threshold=0.02) if len(fib_zones) >= 2 else []

    last_close = closes[-1]
    fib_hit: list[str] = []
    if fib_clusters:
        for fc in fib_clusters[:3]:
            near = abs(last_close - fc["price"]) / max(abs(fc["price"]), 1.0) * 100
            if near < 3:
                fib_hit.append(f"价格{last_close:.2f}在斐波共振区{fc['price']:.2f}({fc['count']}浪段共振，距{near:.1f}%)")
    if not fib_hit and fib_zones:
        z = fib_zones[-1]
        for label, price in z.items():
            if abs(last_close - price) / max(abs(price), 1.0) * 100 < 3:
                fib_hit.append(f"价格在1-3浪{label}位{price:.2f}附近")

    # ── 4. 交替原则 ──
    # 用 _chan_internals 的笔作为波浪子段代理
    chan_int = _chan_internals(daily_data)
    stroke_details = chan_int.get("stroke_details", []) if isinstance(chan_int, dict) else []
    alternation_note = _wave_alternation_principle(stroke_details, sh, sl) if stroke_details else ""

    # ── 5. 通道约束 ──
    ch_status, ch_detail = _wave_channel_constraint(highs, lows, sh, sl)

    # ── 6. 位置：斐波位 + 通道综合 ──
    position_parts: list[str] = []
    if fib_hit:
        position_parts.append(fib_hit[0])
    if ch_status not in ("no_channel",):
        position_parts.append(ch_detail)

    if "pivot_zone" in position_parts or fib_hit:
        position = "key_level"
    elif ch_status in ("upper_channel", "lower_channel"):
        position = "key_level"
    elif ch_status in ("mid_channel",):
        position = "mid_range"
    else:
        position = "mid_range"

    position_detail = " | ".join(position_parts) if position_parts else "无显著斐波/通道参考位"
    if alternation_note:
        position_detail += " | " + alternation_note

    # ── 5. 信号：只在信号方向与趋势一致时发出 ──
    signal, signal_detail = "none", ""
    confidence = 2

    if len(sl) >= 1 and len(sh) >= 1:
        last_sl_idx, last_sl_val = sl_sorted[-1] if sl_sorted else (0, 0)
        last_sh_idx, last_sh_val = sh_sorted[-1] if sh_sorted else (0, 0)

        if last_sl_idx > last_sh_idx and trend in ("up", "neutral"):
            retrace = (last_sh_val - last_sl_val) / last_sh_val * 100 if last_sh_val > 0 else 0
            if 38 <= retrace <= 62:
                signal, signal_detail = "buy", f"回调{retrace:.0f}%至黄金分割区，3浪主升起跳点"
                confidence = 5
            elif 23 <= retrace <= 78:
                signal, signal_detail = "buy", f"回调{retrace:.0f}%至斐波位，{wave_stage}入场"
                confidence = 4
            elif retrace > 62:
                signal, signal_detail = "caution", f"回调{retrace:.0f}%偏深，可能调整未结束"
                confidence = 2

        elif last_sh_idx > last_sl_idx:
            # sell信号仅在趋势向下时发出，且需双重确认
            wave_complete = ("5浪+调整" in wave_stage or "5浪末端" in wave_stage)
            above_ch = (ch_status in ("above_channel", "upper_channel"))
            if trend == "down":
                if wave_complete and above_ch:
                    signal, signal_detail = "sell", f"{wave_detail}+{ch_detail}，双重确认"
                    confidence = 5
                elif wave_complete and ch_status == "mid_channel":
                    signal, signal_detail = "caution", f"{wave_detail}但通道中段，阶段性注意"
                    confidence = 3
                elif above_ch and count_conf >= 4:
                    signal, signal_detail = "sell", f"突破通道上轨+{wave_stage}，过度延伸"
                    confidence = 4
            elif trend == "neutral":
                # 中性趋势下仅双重确认才发caution，不发sell
                if wave_complete and above_ch:
                    signal, signal_detail = "caution", f"{wave_detail}+{ch_detail}，但趋势不配合"
                    confidence = 3

    # 斐波共振增强: buy+fib→conf+1
    if signal == "buy" and fib_clusters and confidence >= 4:
        confidence = 5
        signal_detail += f" + 斐波共振({fib_clusters[0]['count']}段)"

    return EyeVerdict(
        eye="wave", lens="形态·结构",
        trend=trend, trend_detail=trend_detail,
        position=position, position_detail=position_detail,
        signal=signal, signal_detail=signal_detail,
        confidence=confidence,
        horizon="long",  # 浪型完成需10-40日，3浪/5浪不会瞬间展开
    )


# ════════════════════════ 5. 江恩眼 (时间·节奏) ════════════════════════
#
# 江恩理论四支柱：
#   Pillar 1 — 角度线/Fan: ATR标定的1x1线判定趋势强度与方向
#   Pillar 2 — 时间周期: 多转折点斐波时间窗口聚类
#   Pillar 3 — 九方图/价格正方: sqrt→ring→8角度线支撑阻力
#   Pillar 4 — 时空正方: 时间×ATR与价格变动谐波匹配→变盘点


# ── Pillar 3: 九方图 (Square of 9) ──

def _gann_sq9_spiral(price: float) -> dict:
    """将价格映射到九方图螺旋，返回8个角度线价格及最近支撑/阻力。"""
    p = max(abs(price), 0.01)
    sr = math.sqrt(p)
    ring = int(sr)

    # 当前环 + 外环 + 内环的角度线价格
    cardinals: list[tuple[int, str, float]] = []
    diagonals: list[tuple[int, str, float]] = []
    for r_offset in (-2, -1, 0, 1, 2):
        r = ring + r_offset
        if r < 1:
            continue
        cardinals.extend([
            (r, "0°",   (r + 0.0) ** 2),
            (r, "90°",  (r + 0.5) ** 2),
            (r, "180°", (r + 1.0) ** 2),
            (r, "270°", (r + 1.5) ** 2),
        ])
        diagonals.extend([
            (r, "45°",  (r + 0.25) ** 2),
            (r, "135°", (r + 0.75) ** 2),
            (r, "225°", (r + 1.25) ** 2),
            (r, "315°", (r + 1.75) ** 2),
        ])

    all_levels = cardinals + diagonals

    support = None
    resistance = None
    for r, angle, lvl in all_levels:
        if lvl < p and (support is None or lvl > support[2]):
            support = (r, angle, lvl)
        if lvl > p and (resistance is None or lvl < resistance[2]):
            resistance = (r, angle, lvl)

    pct_to_support = (p - support[2]) / p * 100 if support else 999
    pct_to_resistance = (resistance[2] - p) / p * 100 if resistance else 999

    return {
        "ring": ring,
        "price": p,
        "support": support,
        "resistance": resistance,
        "pct_to_support": pct_to_support,
        "pct_to_resistance": pct_to_resistance,
        "cardinals": [(r, a, v) for r, a, v in cardinals if abs(r - ring) <= 1],
        "diagonals": [(r, a, v) for r, a, v in diagonals if abs(r - ring) <= 1],
    }


# ── Pillar 1: 角度线分析 ──

def _gann_fan_analysis(
    highs: list[float], lows: list[float], closes: list[float],
    atr14: list[float | None], n: int,
    sh: list[tuple[int, float]], sl: list[tuple[int, float]],
) -> dict:
    """从最近3个重要转折点绘制江恩角度线，ATR标定价格单位。

    Fan线速率: 1x8=0.125, 1x4=0.25, 1x3=0.333, 1x2=0.5, 1x1=1.0, 2x1=2.0, 3x1=3.0, 4x1=4.0, 8x1=8.0
    从低点向上投影(支撑)，从高点向下投影(阻力)。
    """
    fan_rates = {
        "1x8": 0.125, "1x4": 0.25, "1x3": 0.333, "1x2": 0.5, "1x1": 1.0,
        "2x1": 2.0, "3x1": 3.0, "4x1": 4.0, "8x1": 8.0,
    }

    def _valid_atr() -> float:
        valid_atrs = [v for v in atr14 if v is not None and v > 0]
        return valid_atrs[-1] if valid_atrs else max(closes) * 0.02

    price_unit = _valid_atr()

    # 收集最近3个转折点: 按index降序取最近的不同pivot
    all_candidates: list[tuple[int, float, str]] = []
    for idx, val in sh:
        if idx < n - 1:
            all_candidates.append((idx, val, "high"))
    for idx, val in sl:
        if idx < n - 1:
            all_candidates.append((idx, val, "low"))
    all_candidates.sort(key=lambda x: x[0], reverse=True)

    # 去重: 同一index的不同类型保留
    seen_idx: set[int] = set()
    pivots: list[tuple[int, float, str]] = []
    for idx, val, ptype in all_candidates:
        if idx not in seen_idx:
            seen_idx.add(idx)
            pivots.append((idx, val, ptype))
        if len(pivots) >= 3:
            break

    if not pivots:
        return {
            "trend_signal": "neutral",
            "detail": "无显著转折点",
            "nearest_fan_support": None,
            "nearest_fan_resistance": None,
            "above_1x1_count": 0,
            "below_1x1_count": 0,
            "fan_lines": [],
        }

    last_price = closes[-1]
    above_1x1 = 0
    below_1x1 = 0
    nearest_support = None
    nearest_resistance = None
    fan_line_details: list[str] = []

    for pvt_idx, pvt_price, pvt_type in pivots:
        pu = _valid_atr()
        bars = n - 1 - pvt_idx
        if bars <= 0:
            continue

        direction = 1 if pvt_type == "low" else -1  # low→向上投影, high→向下投影

        for label, rate in fan_rates.items():
            line_val = pvt_price + direction * rate * pu * bars
            label_full = f"{'↑' if direction == 1 else '↓'}{label}"

            if line_val < last_price:
                if nearest_support is None or line_val > nearest_support[0]:
                    nearest_support = (line_val, label_full, pvt_idx, pvt_type)
            elif line_val > last_price:
                if nearest_resistance is None or line_val < nearest_resistance[0]:
                    nearest_resistance = (line_val, label_full, pvt_idx, pvt_type)

        # 1x1判断: 从低点出发price>1x1=强势, 从高点出发price<1x1=弱势
        line_1x1 = pvt_price + direction * 1.0 * pu * bars
        if pvt_type == "low":
            if last_price > line_1x1:
                above_1x1 += 1
                fan_line_details.append(f"从低点{pvt_price:.2f}↑1x1={line_1x1:.2f}，价格在上方(偏多)")
            else:
                below_1x1 += 1
                fan_line_details.append(f"从低点{pvt_price:.2f}↑1x1={line_1x1:.2f}，价格在下方(偏弱)")
        else:
            if last_price < line_1x1:
                below_1x1 += 1
                fan_line_details.append(f"从高点{pvt_price:.2f}↓1x1={line_1x1:.2f}，价格在下方(偏空)")
            else:
                above_1x1 += 1
                fan_line_details.append(f"从高点{pvt_price:.2f}↓1x1={line_1x1:.2f}，价格在上方(偏强)")

    if above_1x1 > below_1x1:
        trend_signal = "bullish"
    elif below_1x1 > above_1x1:
        trend_signal = "bearish"
    else:
        trend_signal = "neutral"

    return {
        "trend_signal": trend_signal,
        "detail": "；".join(fan_line_details[:3]) if fan_line_details else "无有效角度线",
        "nearest_fan_support": nearest_support,
        "nearest_fan_resistance": nearest_resistance,
        "above_1x1_count": above_1x1,
        "below_1x1_count": below_1x1,
    }


# ── Pillar 2: 时间周期聚类 ──

def _gann_time_clusters(pivots: list[tuple[int, float, str]], n: int) -> list[dict]:
    """多转折点斐波时间窗口聚类，≥3重合=强聚类。"""
    fib_numbers = [3, 5, 8, 13, 21, 34, 55, 89, 144, 233]
    hits: dict[int, list[dict]] = {}

    for pvt_idx, pvt_price, pvt_type in pivots:
        bars_elapsed = n - 1 - pvt_idx
        if bars_elapsed < 3:
            continue

        for fib in fib_numbers:
            tol = max(2, int(0.08 * fib))
            if abs(bars_elapsed - fib) <= tol:
                hits.setdefault(fib, []).append({
                    "bars_elapsed": bars_elapsed,
                    "pivot_idx": pvt_idx,
                    "pivot_price": pvt_price,
                    "pivot_type": pvt_type,
                })

    clusters: list[dict] = []
    for fib, entries in hits.items():
        cnt = len(entries)
        if cnt >= 5:
            strength = "strong"
        elif cnt >= 3:
            strength = "moderate"
        else:
            strength = "isolated"
        clusters.append({
            "fib_number": fib,
            "count": cnt,
            "strength": strength,
            "hits": entries,
        })

    clusters.sort(key=lambda c: (c["count"], c["fib_number"]), reverse=True)
    return clusters


# ── Pillar 4: 时空正方 ──

def _gann_time_price_square(
    pivots: list[tuple[int, float, str]],
    closes: list[float],
    atr14: list[float | None],
    n: int,
) -> list[dict]:
    """检测时间×ATR与价格变动的谐波匹配 → 时空正方完成。"""
    harmonic_targets = [1.0, 0.5, 2.0, 0.618, 1.618, 0.333, 3.0, 0.25, 4.0]

    avg_atr_vals = [v for v in atr14[-20:] if v is not None and v > 0]
    avg_atr = sum(avg_atr_vals) / len(avg_atr_vals) if avg_atr_vals else closes[-1] * 0.02
    if avg_atr <= 0:
        return []

    last_price = closes[-1]
    matches: list[dict] = []

    for pvt_idx, pvt_price, pvt_type in pivots:
        time_elapsed = n - 1 - pvt_idx
        if time_elapsed < 8:
            continue
        price_change = abs(last_price - pvt_price)
        price_units = price_change / avg_atr
        if price_units <= 0:
            continue
        ratio = time_elapsed / price_units

        for target in harmonic_targets:
            if target <= 0:
                continue
            closeness = abs(ratio - target) / target
            if closeness < 0.08:
                matches.append({
                    "pivot_idx": pvt_idx,
                    "pivot_price": pvt_price,
                    "pivot_type": pvt_type,
                    "time_elapsed": time_elapsed,
                    "price_units": round(price_units, 2),
                    "actual_ratio": round(ratio, 3),
                    "matched_ratio": target,
                    "closeness": round(closeness, 3),
                })
                break  # 每个pivot只取最匹配的一个target

    matches.sort(key=lambda m: abs(m["matched_ratio"] - 1.0))
    return matches


# ── 季节日检测 ──

def _gann_seasonal_check(trade_date_str: str) -> dict:
    """检查是否接近江恩季节日(二分二至±3天)或节气(±2天)。"""
    try:
        parts = trade_date_str.strip().split("-")
        if len(parts) != 3:
            return {"is_seasonal": False, "seasonal_label": "", "days_to_seasonal": 999}
        m = int(parts[1])
        d = int(parts[2])
    except (ValueError, IndexError):
        return {"is_seasonal": False, "seasonal_label": "", "days_to_seasonal": 999}

    seasonal_dates = [
        (3, 20, "春分"), (6, 21, "夏至"), (9, 22, "秋分"), (12, 21, "冬至"),
        (2, 4, "立春"), (5, 5, "立夏"), (8, 7, "立秋"), (11, 7, "立冬"),
    ]

    best_dist = 999
    best_label = ""
    month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

    def _day_of_year(mo: int, dy: int) -> int:
        return sum(month_days[:mo - 1]) + dy

    doy = _day_of_year(m, d)
    total_days = sum(month_days)

    for sm, sd, label in seasonal_dates:
        sdoy = _day_of_year(sm, sd)
        dist = min(abs(doy - sdoy), total_days - abs(doy - sdoy))
        if dist < best_dist:
            best_dist = dist
            best_label = label

    if best_dist <= 3:
        return {"is_seasonal": True, "seasonal_label": best_label, "days_to_seasonal": best_dist}
    return {"is_seasonal": False, "seasonal_label": best_label if best_dist <= 7 else "",
            "days_to_seasonal": best_dist}


# ════════════════════════ gann_eye 主函数 ════════════════════════

def gann_eye(daily_data: list[dict]) -> EyeVerdict:
    closes = [float(r["close"]) for r in daily_data]
    highs  = [float(r["high"]) for r in daily_data]
    lows   = [float(r["low"]) for r in daily_data]
    volumes = [float(r.get("volume", 0) or 0) for r in daily_data]
    trade_dates = [str(r.get("trade_date", "")) for r in daily_data]
    n = len(daily_data)

    # ── Guard: 最少30日 ──
    if n < 30:
        return EyeVerdict(
            eye="gann", lens="时间·节奏",
            trend="neutral", trend_detail="数据不足(<30日)",
            position="mid_range", position_detail="",
            signal="none", signal_detail="",
            confidence=1,
            horizon="mid",  # 时间窗口指向5-21日，斐波聚类以日/周计
        )

    # ── 预处理 ──
    atr14 = atr(highs, lows, closes, 14)
    ma20 = sma(closes, 20)
    sh, sl = _swing_points(highs, lows, lookback=5)

    # 合并pivot列表（按index排序）
    all_pivots: list[tuple[int, float, str]] = []
    for idx, val in sh:
        all_pivots.append((idx, val, "high"))
    for idx, val in sl:
        all_pivots.append((idx, val, "low"))
    all_pivots.sort(key=lambda x: x[0])

    # ── Pillar 1: 角度线 ──
    fan = _gann_fan_analysis(highs, lows, closes, atr14, n, sh, sl)

    # ── Pillar 2: 时间聚类 ──
    time_clusters = _gann_time_clusters(all_pivots, n)

    # ── Pillar 3: 九方图 ──
    sq9 = _gann_sq9_spiral(closes[-1])

    # ── Pillar 4: 时空正方 ──
    tp_squares = _gann_time_price_square(all_pivots, closes, atr14, n)

    # ── 季节日 ──
    seasonal = _gann_seasonal_check(trade_dates[-1]) if trade_dates else {"is_seasonal": False}

    # ═══════════════════════════════════════════════════════
    # Trend: 角度线主裁决 + MA20/HH-HL副证 + 时间增压
    # ═══════════════════════════════════════════════════════

    trend = "neutral"
    trend_parts: list[str] = []

    # 主裁决: fan趋势信号
    if fan["trend_signal"] == "bullish":
        trend = "up"
        trend_parts.append(f"Fan角度线偏多({fan['above_1x1_count']}枢轴价格在1x1上方)")
    elif fan["trend_signal"] == "bearish":
        trend = "down"
        trend_parts.append(f"Fan角度线偏空({fan['below_1x1_count']}枢轴价格在1x1下方)")
    else:
        trend_parts.append("Fan角度线中性")

    # MA20副证
    ma20_val = ma20[-1] if ma20 and ma20[-1] is not None else 0
    if n >= 25:
        ma20_slope = (ma20_val - (ma20[-6] if ma20[-6] is not None else ma20_val)) / max(abs(ma20_val), 0.01) * 100
    else:
        ma20_slope = 0
    trend_parts.append(f"MA20={ma20_val:.2f}(slope={ma20_slope:.2f}%)")

    # HH/HL or LL/LH 形态检测
    if len(sh) >= 2 and len(sl) >= 2:
        hh = sh[-1][1] > sh[-2][1]
        hl = sl[-1][1] > sl[-2][1]
        ll = sl[-1][1] < sl[-2][1]
        lh = sh[-1][1] < sh[-2][1]

        if hh and hl:
            if trend == "neutral":
                trend = "up"
            trend_parts.append("HH+HL(更高高点+更高低点)")
        elif ll and lh:
            if trend == "neutral":
                trend = "down"
            trend_parts.append("LL+LH(更低低点+更低高点)")
        else:
            trend_parts.append("无明确HH-HL/LL-LH形态")

    # 时间增压: 强时间聚类+季节日可提升中性趋势
    strong_cluster = next((c for c in time_clusters if c["strength"] == "strong"), None)
    if strong_cluster and trend == "neutral" and fan["trend_signal"] == "neutral":
        if ma20_slope > 0.5:
            trend = "up"
            trend_parts.append(f"时间窗口{strong_cluster['fib_number']}天+MA20微升→偏多")
        elif ma20_slope < -0.5:
            trend = "down"
            trend_parts.append(f"时间窗口{strong_cluster['fib_number']}天+MA20微降→偏空")

    trend_detail = "；".join(trend_parts)

    # ═══════════════════════════════════════════════════════
    # Position: SQ9 + Fan S/R + Time Window 三源复合
    # ═══════════════════════════════════════════════════════

    position = "mid_range"
    pos_parts: list[str] = []

    # SQ9距支撑/阻力
    if sq9["pct_to_support"] < 2:
        pos_parts.append(f"SQ9:距{sq9['support'][1]}支撑{sq9['support'][2]:.2f}仅{sq9['pct_to_support']:.1f}%")
    elif sq9["pct_to_resistance"] < 2:
        pos_parts.append(f"SQ9:距{sq9['resistance'][1]}阻力{sq9['resistance'][2]:.2f}仅{sq9['pct_to_resistance']:.1f}%")
    elif sq9["pct_to_support"] < 5:
        pos_parts.append(f"SQ9:接近{sq9['support'][1]}支撑{sq9['support'][2]:.2f}(距{sq9['pct_to_support']:.1f}%)")
    elif sq9["pct_to_resistance"] < 5:
        pos_parts.append(f"SQ9:接近{sq9['resistance'][1]}阻力{sq9['resistance'][2]:.2f}(距{sq9['pct_to_resistance']:.1f}%)")
    else:
        pos_parts.append(f"SQ9:中段(距支{sq9['pct_to_support']:.1f}%/距阻{sq9['pct_to_resistance']:.1f}%)")

    # Fan S/R
    if fan["nearest_fan_support"]:
        sup_val, sup_label, _, _ = fan["nearest_fan_support"]
        sup_pct = (closes[-1] - sup_val) / closes[-1] * 100
        if 0 < sup_pct < 5:
            pos_parts.append(f"Fan:{sup_label}={sup_val:.2f}支撑(距{sup_pct:.1f}%)")
    if fan["nearest_fan_resistance"]:
        res_val, res_label, _, _ = fan["nearest_fan_resistance"]
        res_pct = (res_val - closes[-1]) / closes[-1] * 100
        if 0 < res_pct < 5:
            pos_parts.append(f"Fan:{res_label}={res_val:.2f}阻力(距{res_pct:.1f}%)")

    # 时间窗口
    if strong_cluster:
        pos_parts.append(f"斐波时间:距{strong_cluster['count']}枢轴接近{strong_cluster['fib_number']}天窗口")
    elif time_clusters:
        tc = time_clusters[0]
        pos_parts.append(f"斐波时间:{tc['count']}枢轴接近{tc['fib_number']}天窗口({tc['strength']})")

    # 季节日
    if seasonal.get("is_seasonal"):
        pos_parts.append(f"季节日:{seasonal['seasonal_label']}(距{seasonal['days_to_seasonal']}天)")

    # 位置判定
    near_sq9 = sq9["pct_to_support"] < 2 or sq9["pct_to_resistance"] < 2
    near_fan = (
        (fan["nearest_fan_support"] and (closes[-1] - fan["nearest_fan_support"][0]) / closes[-1] * 100 < 2) or
        (fan["nearest_fan_resistance"] and (fan["nearest_fan_resistance"][0] - closes[-1]) / closes[-1] * 100 < 2)
    ) if (fan["nearest_fan_support"] or fan["nearest_fan_resistance"]) else False

    if near_sq9 or near_fan or (strong_cluster and (near_sq9 or near_fan)):
        position = "key_level"
    elif sq9["pct_to_support"] < 5 or sq9["pct_to_resistance"] < 5:
        position = "approaching"
    elif strong_cluster:
        position = "approaching"
    else:
        position = "mid_range"

    position_detail = " | ".join(pos_parts) if pos_parts else "无显著时间/价格参考位"

    # ═══════════════════════════════════════════════════════
    # Signal: 时空正方 > 时间窗口+角度线 > 季节日+极端角 > 纯时间窗口
    # ═══════════════════════════════════════════════════════

    signal = "none"
    signal_detail = ""
    confidence = 1

    # Priority 1: 时空正方
    tp_match = tp_squares[0] if tp_squares else None
    if tp_match and tp_match["closeness"] < 0.08:
        ptype = tp_match["pivot_type"]
        detail = (
            f"时空正方完成: {ptype}{tp_match['pivot_price']:.2f}后{tp_match['time_elapsed']}天"
            f"价格变动{tp_match['price_units']}单位，比率{tp_match['actual_ratio']}≈{tp_match['matched_ratio']}"
        )
        if ptype == "high":
            signal = "buy"
            signal_detail = detail + "，下行时空平衡→反弹窗口"
        else:
            signal = "sell"
            signal_detail = detail + "，上行时空平衡→回调窗口"
        confidence += 1

    # Priority 2: 时间聚类 + 角度线突破/逼近
    if signal == "none" and strong_cluster:
        fib = strong_cluster["fib_number"]
        if fan["above_1x1_count"] >= 2 and trend == "up":
            signal = "buy"
            signal_detail = (
                f"{fib}天时间窗口+{fan['above_1x1_count']}枢轴价格在1x1上方(强势)，"
                f"时间节点确认上升趋势"
            )
            confidence += 1
        elif fan["below_1x1_count"] >= 3 and trend == "down":
            signal = "sell"
            signal_detail = (
                f"{fib}天时间窗口+{fan['below_1x1_count']}枢轴价格在1x1下方(弱势)，"
                f"时间节点确认下降趋势"
            )
            confidence += 1

    # Priority 3: 季节日 + 极端角度
    if signal == "none" and seasonal.get("is_seasonal"):
        fan_detail_text = fan.get("detail", "")
        if "1x4" in fan_detail_text or "4x1" in fan_detail_text or "8x1" in fan_detail_text:
            signal = "caution"
            signal_detail = f"{seasonal['seasonal_label']}前后+极端角度线，注意变盘"
            confidence += 1

    # Priority 4: 强时间窗口无角度确认 (不发sell了，只buy/caution)
    if signal == "none" and strong_cluster:
        fib = strong_cluster["fib_number"]
        if closes[-1] < closes[-5] * (1 - 0.03):
            signal = "buy"
            signal_detail = f"{fib}天时间窗口({strong_cluster['count']}枢轴共振)+近期回调，关注反弹"
            confidence += 1
        else:
            signal = "caution"
            signal_detail = f"{fib}天时间窗口({strong_cluster['count']}枢轴共振)，等待方向选择"
            confidence += 1

    # Priority 5: 中等时间窗口
    if signal == "none":
        moderate = next((c for c in time_clusters if c["strength"] == "moderate"), None)
        if moderate:
            signal = "caution"
            signal_detail = f"接近{moderate['fib_number']}天时间窗口({moderate['count']}枢轴)，适度关注"
            confidence += 0

    if signal == "none":
        signal_detail = "无显著时间信号，维持现有趋势判断"

    # ═══════════════════════════════════════════════════════
    # Confidence 评分
    # ═══════════════════════════════════════════════════════

    # ── 累积评分 (每种独立证据+1，上限5) ──
    confirmations = 0

    if strong_cluster and strong_cluster["count"] >= 5:
        confirmations += 1
    if tp_match and tp_match["closeness"] < 0.06:
        confirmations += 1
    if sq9["pct_to_support"] < 1.5 or sq9["pct_to_resistance"] < 1.5:
        confirmations += 1
    if seasonal.get("is_seasonal"):
        confirmations += 1
    if moderate_cluster := next((c for c in time_clusters if c["strength"] in ("moderate", "strong")), None):
        confirmations += 1  # 任意有效时间窗口+1

    confidence = min(5, 1 + confirmations)
    confidence = max(2, confidence)  # 最低2（因为至少有一些数据）

    return EyeVerdict(
        eye="gann", lens="时间·节奏",
        trend=trend, trend_detail=trend_detail,
        position=position, position_detail=position_detail,
        signal=signal, signal_detail=signal_detail,
        confidence=confidence,
        horizon="mid",  # 时间窗口指向5-21日，斐波聚类以日/周计
    )


# ════════════════════════ 共识器 (置信度加权投票 + 优势方向识别 + 冲突消解) ════════════════════════

# ═══════════════════════════════════════════════════════
# 权重持久化
# ═══════════════════════════════════════════════════════

_HARDCODED_TREND = {
    "candle":    {"up": 0.28, "down": 0.26},
    "indicator": {"up": 0.34, "down": 0.31},
    "chan":      {"up": 0.39, "down": 0.36},
    "wave":      {"up": 0.39, "down": 0.37},
    "gann":      {"up": 0.42, "down": 0.39},
}

_HARDCODED_SIGNAL = {
    "candle":    {"buy": 0.30, "sell": 0.27},
    "indicator": {"buy": 0.37, "sell": 0.39},
    "chan":      {"buy": 0.39, "sell": 0.37},
    "wave":      {"buy": 0.34, "sell": 0.42},
    "gann":      {"buy": 0.40, "sell": 0.40},
}

_WEIGHTS_CACHE: dict | None = None
_WEIGHTS_CACHE_MTIME: float = 0


def _load_weights():
    """从 JSON 加载权重，失败则返回硬编码默认值。文件修改时自动重载。"""
    global _WEIGHTS_CACHE, _WEIGHTS_CACHE_MTIME
    weights_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'eye_weights.json')
    try:
        mtime = os.path.getmtime(weights_path)
        if _WEIGHTS_CACHE is not None and mtime == _WEIGHTS_CACHE_MTIME:
            return _WEIGHTS_CACHE["trend_weight"], _WEIGHTS_CACHE["signal_weight"]
        with open(weights_path, encoding='utf-8') as f:
            data = json.load(f)
        _WEIGHTS_CACHE = data
        _WEIGHTS_CACHE_MTIME = mtime
        return data.get("trend_weight", _HARDCODED_TREND), data.get("signal_weight", _HARDCODED_SIGNAL)
    except Exception:
        return _HARDCODED_TREND, _HARDCODED_SIGNAL


def _turnover_surge(turnover_rates: list[float], window: int = 60, pct: float = 0.9) -> bool:
    """换手率突增：当日换手率 >= 近 window 日 90 分位（需 >=30 个有效值）。

    已验证：换手率突增不独立，是量比>2 的放大器——仅在量比>2 基础上增强信号（+7pp）。
    """
    valid = [v for v in turnover_rates[-window:] if v and v > 0]
    if len(valid) < 30:
        return False
    return valid[-1] >= sorted(valid)[int(pct * len(valid))]


def volume_retreat_alert(daily_data: list[dict], turnover_rates: list[float] | None = None) -> dict:
    """量比退潮预警——「极端放量 = 见顶」这条已验证 alpha 的落地。

    量比 = 当日成交量 / 近20日均量（含当日，与 market_cap_volume_test 口径一致）。
    已验证: 量比>2 放量 → 短线看跌 +10.7pp(5日)，大盘股 +14.4pp。
    形态: 放量大涨日 = 见顶日，退潮风险高。

    2026-08-14 升级：加「换手率突增」作为确认因子（2×2 分解验证）：
      - 量比>2 且涨 且换手率突增 → strong（双触发，看跌 +13.7pp 最强）
      - 量比>2 且涨（无换手率数据/未突增）→ high
      - 量比>2 但未涨 → low

    返回 {triggered, vol_ratio, pct_chg, turnover_surge, level, message}，
    level: none/low/high/strong。
    """
    if len(daily_data) < 21:
        return {"triggered": False, "vol_ratio": 0.0, "pct_chg": 0.0, "turnover_surge": False, "level": "none", "message": ""}

    vols = [float(r.get("volume", 0) or 0) for r in daily_data[-20:]]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    cur_vol = float(daily_data[-1].get("volume", 0) or 0)
    pct_chg = float(daily_data[-1].get("pct_chg", 0) or 0)

    vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 0.0
    turnover_surge = _turnover_surge(turnover_rates) if turnover_rates else False

    if vol_ratio >= 2.0 and pct_chg > 0 and turnover_surge:
        level = "strong"
        message = f"量比 {vol_ratio} 且当日涨 {pct_chg:.1f}% 且换手率突增——极端放量+高换手，历史统计看跌概率最高，短线退潮强预警"
    elif vol_ratio >= 3.0 and pct_chg > 0:
        level = "high"
        message = f"量比 {vol_ratio} 且当日涨 {pct_chg:.1f}%——极端放量大涨，历史统计显示这是见顶形态，短线退潮风险高"
    elif vol_ratio >= 2.0 and pct_chg > 0:
        level = "high"
        message = f"量比 {vol_ratio} 且当日涨 {pct_chg:.1f}%——放量大涨，警惕见顶退潮"
    elif vol_ratio >= 2.0:
        level = "low"
        message = f"量比 {vol_ratio} 放量但当日未涨，退潮信号偏弱"
    else:
        level = "none"
        message = ""

    return {
        "triggered": level != "none",
        "vol_ratio": vol_ratio,
        "pct_chg": round(pct_chg, 2),
        "turnover_surge": turnover_surge,
        "level": level,
        "message": message,
    }


def consensus(daily_data: list[dict], turnover_rates: list[float] | None = None) -> ConsensusResult:
    eyes = {
        "candle":    candle_eye(daily_data),
        "indicator": indicator_eye(daily_data),
        "chan":      chan_eye(daily_data),
        "wave":      wave_eye(daily_data),
        "gann":      gann_eye(daily_data),
    }

    _trend_weight, _signal_weight = _load_weights()

    # horizon 只做信息展示，不参与权重计算——命中率已包含窗口差异
    # _horizon_groups / _horizon_trend_w / _horizon_signal_w 已移除 (2026-08-12)

    # ═══════════════════════════════════════════════════════
    # Trend: 置信度 × 方向命中率 投票
    # ═══════════════════════════════════════════════════════

    trend_scores: dict[str, float] = {"up": 0, "down": 0, "neutral": 0}
    trend_breakdown: dict[str, list[str]] = {"up": [], "down": [], "neutral": []}

    for name, e in eyes.items():
        tw = _trend_weight.get(name, {"up": 0.5, "down": 0.5})
        w = e.confidence * tw.get(e.trend, 0.3)
        trend_scores[e.trend] += w
        trend_breakdown.setdefault(e.trend, []).append(name)

    # 去重
    for k in trend_breakdown:
        trend_breakdown[k] = list(set(trend_breakdown[k]))

    total = sum(trend_scores.values())
    up_share = trend_scores["up"] / total if total > 0 else 0
    down_share = trend_scores["down"] / total if total > 0 else 0

    if up_share > 0.50:
        best_trend = "up"
    elif down_share > 0.50:
        best_trend = "down"
    else:
        best_trend = "neutral"

    trend_votes = len(trend_breakdown.get(best_trend, []))
    trend_r = {
        "verdict": best_trend,
        "votes": f"{trend_votes}/5",
        "detail": _vote_detail("trend", best_trend, trend_breakdown),
        "breakdown": {k: sorted(v) for k, v in trend_breakdown.items() if v},
    }

    # ═══════════════════════════════════════════════════════
    # Position: 置信度加权投票
    # ═══════════════════════════════════════════════════════

    pos_tally: dict[str, list[str]] = {}
    pos_scores: dict[str, float] = {"key_level": 0, "approaching": 0, "mid_range": 0}
    for name, e in eyes.items():
        pos_tally.setdefault(e.position, []).append(name)
        pos_scores[e.position] += e.confidence

    best_pos = max(pos_scores, key=pos_scores.get)
    pos_count = len(pos_tally[best_pos])
    position_r = {
        "verdict": best_pos,
        "votes": f"{pos_count}/5",
        "detail": _vote_detail("position", best_pos, pos_tally),
        "breakdown": {k: sorted(v) for k, v in pos_tally.items() if v},
    }

    # ═══════════════════════════════════════════════════════
    # Signal: 置信度 × 方向命中率 投票 + 加权冲突消解
    # ═══════════════════════════════════════════════════════

    signal_scores: dict[str, float] = {"buy": 0, "sell": 0, "caution": 0, "none": 0}
    signal_breakdown: dict[str, list[str]] = {"buy": [], "sell": [], "caution": [], "none": []}
    signal_eye_names: dict[str, int] = {"buy": 0, "sell": 0, "caution": 0}

    for name, e in eyes.items():
        sw = _signal_weight.get(name, {"buy": 0.5, "sell": 0.5})
        w = e.confidence * sw.get(e.signal, 0.2) if e.signal != "none" else 0.3
        if e.signal != "none":
            signal_scores[e.signal] += w
            signal_eye_names[e.signal] += 1
        signal_breakdown[e.signal].append(name)

    # 冲突消解: 买卖冲突时不数眼数，比命中率加权的分数
    buy_eyes = signal_eye_names["buy"]
    sell_eyes = signal_eye_names["sell"]
    if buy_eyes >= 2 and sell_eyes >= 2:
        # buy卖方所有眼的命中率加权分之和，sell方同理
        buy_score = signal_scores["buy"]
        sell_score = signal_scores["sell"]
        if buy_score > sell_score * 1.2:
            best_sig = "buy"
            conflict_note = f"买卖冲突({buy_eyes}:{sell_eyes}眼)，加权分buy({buy_score:.1f})>sell({sell_score:.1f})"
        elif sell_score > buy_score * 1.2:
            best_sig = "sell"
            conflict_note = f"买卖冲突({buy_eyes}:{sell_eyes}眼)，加权分sell({sell_score:.1f})>buy({buy_score:.1f})"
        else:
            best_sig = "caution"
            conflict_note = f"买卖冲突({buy_eyes}:{sell_eyes}眼)，加权分接近({buy_score:.1f} vs {sell_score:.1f})"
    else:
        best_sig = max(signal_scores, key=signal_scores.get)
        conflict_note = ""

    if best_sig == "none" and any(signal_scores[s] > 0 for s in ("buy", "sell", "caution")):
        best_sig = max(("buy", "sell", "caution"), key=lambda s: signal_scores[s])

    signal_votes = len(signal_breakdown.get(best_sig, []))
    signal_r = {
        "verdict": best_sig,
        "votes": f"{signal_votes}/5",
        "weighted": best_sig,
        "detail": _vote_detail("signal", best_sig, signal_breakdown),
        "breakdown": {k: sorted(v) for k, v in signal_breakdown.items() if v},
    }

    # ═══════════════════════════════════════════════════════
    # 摘要生成
    # ═══════════════════════════════════════════════════════

    eye_names_map = {
        "candle": "蜡烛", "indicator": "指标", "chan": "缠论",
        "wave": "波浪", "gann": "江恩",
    }
    trend_eyes_zh = [eye_names_map[n] for n in trend_breakdown.get(best_trend, [])]
    pos_eyes_zh = [eye_names_map[n] for n in pos_tally.get(best_pos, [])]
    sig_eyes_zh = [eye_names_map[n] for n in signal_breakdown.get(best_sig, [])]

    trend_zh = {"up": "看多", "down": "看空", "neutral": "中性"}
    pos_zh = {"key_level": "关键位", "approaching": "接近关键位", "mid_range": "中段"}
    sig_zh = {"buy": "买入信号", "sell": "卖出信号", "caution": "注意风险", "none": "无明确信号"}

    # 信号共振强度 (加权分)
    sig_intensity = signal_scores.get(best_sig, 0)
    if sig_intensity >= 12 and best_sig in ("buy", "sell"):
        note = f"多眼强共振{ '买入' if best_sig == 'buy' else '卖出' }，置信度高。"
    elif sig_intensity >= 7 and best_sig in ("buy", "sell"):
        note = f"多眼一致{ '看多' if best_sig == 'buy' else '看空' }，可参考。"
    elif sig_intensity >= 5 and best_sig in ("buy", "sell"):
        note = f"部分眼倾向{ '买入' if best_sig == 'buy' else '卖出' }，轻仓参考。"
    elif best_sig == "caution":
        if conflict_note:
            note = f"{conflict_note}，方向不明，观望。"
        else:
            note = "几双眼睛都看到了异常，等待方向明确。"
    elif best_sig == "none":
        note = "各眼信号不一，当前位置无合力，耐心等待。"
    else:
        note = ""

    summary = (
        f"趋势：{trend_r['votes']}眼{trend_zh[best_trend]}"
        f"（{'、'.join(trend_eyes_zh)}），"
        f"位置：{position_r['votes']}眼判断在{pos_zh[best_pos]}"
        f"（{'、'.join(pos_eyes_zh)}），"
        f"信号：{sig_zh.get(best_sig, best_sig)}"
        f"（{'、'.join(sig_eyes_zh)}）。{note}"
    )

    # 白话总结 — 逐眼翻译
    plain_summary = _generate_plain_summary(eyes, trend_r, position_r, signal_r, sig_intensity, best_sig)

    return ConsensusResult(
        eyes=eyes,
        trend=trend_r,
        position=position_r,
        signal=signal_r,
        summary=summary,
        plain_summary=plain_summary,
        retreat_alert=volume_retreat_alert(daily_data, turnover_rates),
    )


def _vote_detail(field: str, verdict: str, tally: dict[str, list[str]]) -> str:
    """生成投票详情人话。"""
    eye_names = {
        "candle": "蜡烛", "indicator": "指标", "chan": "缠论",
        "wave": "波浪", "gann": "江恩",
    }
    supporting = [eye_names[n] for n in tally.get(verdict, [])]
    opposing = []
    for v, names in tally.items():
        if v != verdict and v != "none" and v != "neutral":
            opposing.extend(eye_names[n] for n in names)
    if not opposing:
        opposing = [eye_names[n] for n in tally.get("neutral", []) + tally.get("none", [])]
    return f"{'、'.join(supporting)}一致{'，'.join(opposing)}持不同看法" if opposing else f"{'、'.join(supporting)}一致"


def _generate_plain_summary(
    eyes: dict[str, EyeVerdict],
    trend_r: dict,
    position_r: dict,
    signal_r: dict,
    sig_intensity: float,
    best_sig: str,
) -> str:
    """按时间线分层解读：超短→短→中→长，每层汇总该窗口眼睛的看法。"""

    # 按验证窗口分组
    horizon_windows = {
        "short": ("3~5日", ["candle"]),
        "mid": ("1~3周", ["indicator", "gann"]),
        "long": ("1~2月", ["chan"]),
        "xlong": ("2~3月", ["wave"]),
    }

    lines: list[str] = []
    # 时间线分组解读
    for h_key, (label, names) in horizon_windows.items():
        group_eyes = [n for n in names if n in eyes]
        if not group_eyes:
            continue

        group_lines: list[str] = []
        for name in group_eyes:
            group_lines.append(_time_lens_eye(name, eyes[name]))
        if not group_lines:
            continue

        lines.append(f"【{label}信号】")
        lines.extend(group_lines)

    # 综合一句
    lines.append("")
    lines.append(_verdict_line(trend_r, position_r, signal_r, sig_intensity, best_sig))

    return "\n".join(lines)


def _time_lens_eye(name: str, e: EyeVerdict) -> str:
    """从时间维度解读一只眼——这只看什么，在这个时间窗口下看到了什么。"""
    labels = {
        "candle": "蜡烛眼",
        "indicator": "指标眼",
        "chan": "缠论眼",
        "wave": "波浪眼",
        "gann": "江恩眼",
    }
    label = labels.get(name, name)
    trend_map = {"up": "看涨", "down": "看跌", "neutral": "横盘震荡"}
    sig_map = {"buy": "→ 给出买入信号", "sell": "→ 给出卖出信号", "caution": "→ 建议谨慎观望", "none": "→ 无明确交易信号"}

    t = trend_map.get(e.trend, e.trend)
    s = sig_map.get(e.signal, "")

    # 自信度
    if e.confidence >= 8:
        conf_word = "非常确信"
    elif e.confidence >= 5:
        conf_word = "比较有把握地"
    elif e.confidence >= 3:
        conf_word = ""
    else:
        conf_word = "微弱地"

    # 趋势方向
    trend_part = f"{conf_word}{t}"
    if e.signal in ("buy", "sell"):
        return f"  {label} {trend_part}{s}"
    elif e.signal == "caution":
        return f"  {label} {trend_part}，但注意到异常{s}"
    else:
        return f"  {label} {trend_part}，未触发交易信号"


def _verdict_line(
    trend_r: dict,
    position_r: dict,
    signal_r: dict,
    sig_intensity: float,
    best_sig: str,
) -> str:
    """综合一句话：时间维度交叉验证的结论。"""
    parts: list[str] = []

    t = trend_r["verdict"]
    if t == "up":
        parts.append("3~5日短线偏多，1~3周中线偏多，1~2月长线也偏多——大中小周期方向一致")
    elif t == "down":
        parts.append("短线到长线整体偏空，各周期方向统一向下")
    else:
        parts.append("短期和中长期方向分歧，短线和长线不在同一节奏上")

    p = position_r["verdict"]
    if p == "key_level":
        parts.append("当前处于关键支撑/阻力位，各周期信号在这个位置容易集中爆发，变盘概率增加")
    elif p == "approaching":
        parts.append("价格正在逼近关键位，未来几天需要密切关注是否突破或受阻")
    else:
        parts.append("价格在中间区域运行，没有关键位压力，趋势延续的可能性较大")

    s = signal_r["verdict"]
    if s == "buy" and sig_intensity >= 12:
        parts.append("超短至长线多只眼睛同时喊买，共振力度很强，是值得重视的做多节点")
    elif s == "buy" and sig_intensity >= 7:
        parts.append("多个时间窗口的眼睛都看到买入信号，做多信号有一定可信度")
    elif s == "buy":
        parts.append("有买入信号但不够一致——可能只是短期反弹机会，不宜重仓")
    elif s == "sell" and sig_intensity >= 12:
        parts.append("多周期共振卖出，从超短到长线都在提示风险，建议认真考虑减仓")
    elif s == "sell" and sig_intensity >= 7:
        parts.append("多个窗口发出卖出信号，下跌风险在累积，可以逐步减仓")
    elif s == "sell":
        parts.append("有零散卖出信号但未形成多周期合力，不一定马上跌，但值得留意")
    elif s == "caution":
        parts.append("当前各时间窗口看法矛盾较大，没有形成一致方向，最好等明朗后再动手")
    else:
        parts.append("各周期都没有明确交易信号，当前位置不适合强行入场，耐心等待")

    return "整体判断：" + "。".join(parts) + "。"
