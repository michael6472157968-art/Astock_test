"""诊股 API——个股短线技术分析（T+3至T+7），含量化信号、K线结构、关键价位。"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from app.core.cache import cache_get, cache_set
from app.core.settings import get_settings
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/diagnosis", tags=["诊股"])
_settings = get_settings()

# ── 技术指标计算 ──

def _sma(values: list[float], n: int) -> list[float]:
    out = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(sum(values[i - n + 1:i + 1]) / n)
    return out


def _ema(values: list[float], n: int) -> list[float]:
    out = []
    k = 2 / (n + 1)
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
        else:
            out.append(v * k + out[-1] * (1 - k))
    return out


def _macd(closes: list[float]) -> dict:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b if a is not None and b is not None else None for a, b in zip(ema12, ema26)]
    dea = _ema([d if d is not None else 0 for d in dif], 9)
    macd_bar = [(d - dea[i]) * 2 if d is not None else None for i, d in enumerate(dif)]
    return {"dif": dif, "dea": dea, "bar": macd_bar}


def _rsi(closes: list[float], n: int = 14) -> list[float]:
    out = []
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    for i in range(n, len(gains) + 1):
        avg_gain = sum(gains[i - n:i]) / n
        avg_loss = sum(losses[i - n:i]) / n
        if avg_loss == 0:
            out.append(100)
        else:
            rs = avg_gain / avg_loss
            out.append(round(100 - 100 / (1 + rs), 2))
    return out  # length = len(closes) - 1 - n + 1


def _kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> dict:
    k_vals, d_vals, j_vals = [], [], []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
        k_prev = k_vals[-1] if k_vals else 50
        d_prev = d_vals[-1] if d_vals else 50
        k = k_prev * 2 / 3 + rsv / 3
        d = d_prev * 2 / 3 + k / 3
        j = 3 * k - 2 * d
        k_vals.append(round(k, 2))
        d_vals.append(round(d, 2))
        j_vals.append(round(j, 2))
    return {"k": k_vals, "d": d_vals, "j": j_vals}


def _bollinger(closes: list[float], n: int = 20) -> dict:
    ma = _sma(closes, n)
    upper, lower = [], []
    for i in range(len(ma)):
        if ma[i] is None:
            upper.append(None); lower.append(None)
        else:
            std = _stddev(closes[i - n + 1:i + 1], ma[i])
            upper.append(round(ma[i] + 2 * std, 2))
            lower.append(round(ma[i] - 2 * std, 2))
    return {"mid": ma, "upper": upper, "lower": lower}


def _stddev(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        a = highs[i] - lows[i]
        b = abs(highs[i] - closes[i - 1])
        c = abs(lows[i] - closes[i - 1])
        trs.append(max(a, b, c))
    return _sma(trs, n)


def _pivot_points(highs, lows, closes, lookback=20):
    """支撑/阻力：布林下轨/上轨 + 20日最低/最高 + ATR止损止盈 + 前高前低"""
    close = closes[-1]
    hh = max(highs)
    ll = min(lows)
    atr_val = _atr(highs, lows, closes, 14)[-1]
    if atr_val is None:
        atr_val = close * 0.02
    return {
        "support_price": round(ll, 2),
        "support_desc": "20日最低价",
        "resistance_price": round(hh, 2),
        "resistance_desc": "20日最高价",
        "stop_loss_price": round(close - atr_val * 2, 2),
        "take_profit_price_1": round(close + atr_val * 3, 2),
        "take_profit_price_2": round(close + atr_val * 5, 2),
        "atr": round(atr_val, 2),
    }


# ── 量化信号生成 ──

def _quant_signal(closes, highs, lows, volumes) -> dict:
    """综合技术指标 → 量化信号"""
    macd = _macd(closes)
    rsi = _rsi(closes)
    kdj = _kdj(highs, lows, closes)
    boll = _bollinger(closes)
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    atr = _atr(highs, lows, closes)
    pivot = _pivot_points(highs, lows, closes)

    last = len(closes) - 1
    dif = macd["dif"][last] or 0
    dea = macd["dea"][last] or 0
    bar = macd["bar"][last] or 0
    rsi_now = rsi[-1] if rsi else 50
    k_now = kdj["k"][-1] if kdj["k"] else 50
    d_now = kdj["d"][-1] if kdj["d"] else 50
    j_now = kdj["j"][-1] if kdj["j"] else 50
    close = closes[-1]
    boll_mid = boll["mid"][-1] or close
    boll_upper = boll["upper"][-1] or close
    boll_lower = boll["lower"][-1] or close
    atr_now = pivot["atr"]
    vol_ratio = volumes[-1] / (sum(volumes[-20:]) / 20) if len(volumes) >= 20 and volumes[-1] > 0 else 1

    # 量价
    vol_signals = []
    if vol_ratio > 2:
        vol_signals.append("放量(>2倍均量)")
    elif vol_ratio > 1.3:
        vol_signals.append("温和放量")
    elif vol_ratio < 0.5:
        vol_signals.append("缩量(<0.5倍均量)")

    # 趋势
    trend_signals = []
    if ma5[-1] is not None and ma20[-1] is not None:
        if ma5[-1] > ma20[-1]:
            trend_signals.append("短多排列(MA5>MA20)")
        else:
            trend_signals.append("短空排列(MA5<MA20)")
    if dif > dea:
        trend_signals.append("MACD金叉区域" if bar > 0 else "MACD多头收敛")
    else:
        trend_signals.append("MACD死叉区域" if bar < 0 else "MACD空头收敛")

    # 超买超卖
    over_signals = []
    if rsi_now > 80:
        over_signals.append("RSI超买({:.0f})".format(rsi_now))
    elif rsi_now < 20:
        over_signals.append("RSI超卖({:.0f})".format(rsi_now))
    else:
        over_signals.append("RSI中性({:.0f})".format(rsi_now))
    if j_now > 100:
        over_signals.append("KDJ超买")
    elif j_now < 0:
        over_signals.append("KDJ超卖")
    else:
        over_signals.append("KDJ中性")

    # 布林位置
    boll_pos = (close - boll_lower) / (boll_upper - boll_lower) if boll_upper != boll_lower else 0.5
    if boll_pos > 0.9:
        boll_signal = "触及布林上轨"
    elif boll_pos < 0.1:
        boll_signal = "触及布林下轨"
    else:
        boll_signal = "布林中轨区域" if 0.4 < boll_pos < 0.6 else "布林偏强" if boll_pos > 0.5 else "布林偏弱"

    # 成交量趋势
    if len(volumes) >= 10:
        vol_ma5 = sum(volumes[-5:]) / 5
        vol_ma10 = sum(volumes[-10:]) / 10
        vol_trend = "量能放大" if vol_ma5 > vol_ma10 * 1.2 else "量能萎缩" if vol_ma5 < vol_ma10 * 0.8 else "量能平稳"
    else:
        vol_trend = "数据不足"

    # 综合评分与风险
    score, risk = _calc_quant_score(rsi_now, k_now, d_now, j_now, dif, dea, bar, boll_pos, vol_ratio)
    suggestion = _gen_suggestion(score, risk, close, pivot)

    return {
        "calc_date": date.today().isoformat(),
        "close": round(close, 2),
        "score": score,
        "risk": risk,
        "indicators": {
            "macd": {"dif": round(dif, 4), "dea": round(dea, 4), "bar": round(bar, 4)},
            "rsi": rsi_now,
            "kdj": {"k": k_now, "d": d_now, "j": round(j_now, 2)},
            "boll": {"mid": round(boll_mid, 2), "upper": round(boll_upper, 2), "lower": round(boll_lower, 2), "position": round(boll_pos * 100, 1)},
            "ma5": round(ma5[-1], 2) if ma5[-1] is not None else None,
            "ma10": round(ma10[-1], 2) if ma10[-1] is not None else None,
            "ma20": round(ma20[-1], 2) if ma20[-1] is not None else None,
            "atr": atr_now,
            "vol_ratio": round(vol_ratio, 2),
            "vol_trend": vol_trend,
        },
        "signals": {
            "volume": vol_signals,
            "trend": trend_signals,
            "overbought_oversold": over_signals,
            "bollinger": boll_signal,
        },
        "key_levels": pivot,
        "suggestion": suggestion,
    }


def _calc_quant_score(rsi, k, d, j, dif, dea, bar, boll_pos, vol_ratio):
    score = 50
    # RSI
    if rsi > 80: score -= 15
    elif rsi > 70: score -= 5
    elif rsi < 20: score += 15
    elif rsi < 30: score += 5
    # KDJ
    if j > 100: score -= 5
    elif j < 0: score += 10
    elif k > d and j > 50: score += 5
    # MACD
    if dif > dea and bar > 0: score += 8
    elif dif < dea and bar < 0: score -= 8
    elif dif > dea: score += 3
    elif dif < dea: score -= 3
    # 布林
    if boll_pos > 0.9: score -= 5
    elif boll_pos < 0.1: score += 10
    # 量
    if vol_ratio > 2: score += 5
    elif vol_ratio < 0.5: score -= 3

    score = max(1, min(99, score))

    if score >= 75: risk = "低风险"
    elif score >= 60: risk = "中低风险"
    elif score >= 40: risk = "中风险"
    elif score >= 25: risk = "中高风险"
    else: risk = "高风险"

    return score, risk


def _gen_suggestion(score, risk, close, pivot):
    if score >= 75:
        action = "短线看多信号明确，可轻仓参与T+3至T+5短线操作"
    elif score >= 55:
        action = "短线偏多但存在不确定因素，建议观察1-2个交易日确认方向后轻仓试多"
    elif score >= 40:
        action = "短线方向不明确，建议观望等待信号明朗化"
    elif score >= 25:
        action = "短线偏空，已有仓位注意设好止损，不宜新开仓位"
    else:
        action = "短线看空信号较强，宜减仓或空仓观望"

    return {
        "action": action,
        "risk_level": risk,
        "target_price": pivot.get("take_profit_price_1"),
        "stop_loss": pivot.get("stop_loss_price"),
        "hold_period": "T+3 至 T+7",
        "position_advice": "轻仓(20-30%)" if score >= 55 else "观望" if score >= 40 else "减仓/空仓",
    }


# ── K线结构数据 ──

def _build_kline_data(df) -> dict:
    """将Tushare日线DataFrame转为前端ECharts可渲染的K线JSON"""
    kline = []
    for _, row in df.iterrows():
        kline.append([
            str(row.get("trade_date", "")),
            round(float(row.get("open", 0) or 0), 2),
            round(float(row.get("close", 0) or 0), 2),
            round(float(row.get("low", 0) or 0), 2),
            round(float(row.get("high", 0) or 0), 2),
            round(float(row.get("vol", 0) or 0), 0),
        ])
    kline.reverse()  # 最旧在前，ECharts candlestick 格式
    return {"columns": ["trade_date", "open", "close", "low", "high", "volume"], "rows": kline}


# ── API ──

@router.get("/{stock_code}")
async def get_diagnosis(stock_code: str, request: Request):
    tier = _get_tier(request)
    cache_key = f"diag_v2:{stock_code}"
    cached = await cache_get(cache_key)
    if cached:
        return _build_response(cached, tier, cache_hit=True)

    report = await _compute_diagnosis(stock_code)
    if report is None:
        raise HTTPException(status_code=404, detail=f"股票 {stock_code} 暂无数据")

    await cache_set(cache_key, report, ttl=_settings.cache_diagnosis_ttl)
    return _build_response(report, tier, cache_hit=False)


async def _compute_diagnosis(stock_code: str) -> dict | None:
    """从Tushare获取60天日线数据 → 计算技术指标 → 生成报告"""
    from app.services.tushare_client import get_pro

    try:
        pro = get_pro()
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=60)).strftime("%Y%m%d")

        for suffix in [".SZ", ".SH", ""]:
            code = stock_code if suffix == "" else (stock_code if "." in stock_code else stock_code + suffix)
            df = pro.daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and not df.empty:
                break

        if df is None or df.empty:
            return None

        closes = [float(r.close) for _, r in df.iterrows() if r.close and float(r.close) > 0]
        highs  = [float(r.high) for _, r in df.iterrows() if r.high]
        lows   = [float(r.low) for _, r in df.iterrows() if r.low]
        opens  = [float(r.open) for _, r in df.iterrows() if r.open]
        vols   = [float(r.vol) for _, r in df.iterrows() if r.vol]

        if len(closes) < 20:
            return None

        closes.reverse(); highs.reverse(); lows.reverse(); opens.reverse(); vols.reverse()

        quant = _quant_signal(closes, highs, lows, vols)
        kline = _build_kline_data(df)

        # 获取股票名称
        stock_name = stock_code
        try:
            from app.services.tushare_client import get_stock_basic
            basics = await get_stock_basic()
            for b in basics:
                if b.get("ts_code") == code:
                    stock_name = b.get("name", stock_code)
                    break
        except Exception:
            pass

        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "quant": quant,
            "kline": kline,
        }
    except Exception:
        return None


def _build_response(report: dict, tier: int, cache_hit: bool) -> APIResponse:
    data = {
        "stock_code": report["stock_code"],
        "stock_name": report["stock_name"],
        "quant": report["quant"],
        "kline": report.get("kline"),
    }

    return APIResponse(
        data=data,
        timestamp=int(time.time()),
        ext_info={"cache_hit": cache_hit},
    )


def _get_tier(request: Request) -> int:
    return getattr(request.state, "tier", 0) if hasattr(request.state, "tier") else 0
