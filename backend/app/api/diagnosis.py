"""诊股 API——个股短线技术分析（T+3至T+7），含量化信号、K线结构、关键价位。"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.cache import cache_get, cache_set
from app.core.database import async_session
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth_optional, require_auth
from app.models.orm.models import CreditLedger, User
from app.models.schemas.common import APIResponse
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/diagnosis", tags=["诊股"])
_settings = get_settings()

from app.services.factor_lib import (
    sma as _sma, ema as _ema, macd as _macd, rsi as _rsi,
    kdj as _kdj, bollinger as _bollinger, atr as _atr,
)


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
    # 估值/情绪数据由上层调用者传入（_compute_diagnosis 不持有），
    # 此处提供兼容层：None 时四维度评分的估值/情绪维度退化为 50 分。
    # 上层 fetch 完成后会通过 report["quant"]["dimensions"] 更新。
    score, risk = _calc_compat_score(rsi_now, k_now, d_now, j_now, dif, dea, bar, boll_pos, vol_ratio)
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


# ── 四维度评分引擎 ──

def _score_valuation(daily_basic: dict | None) -> dict:
    """估值维度评分(0-100): PE合理性 + PB合理性。分数越高=越低估。"""
    drivers: list[str] = []
    score = 50.0

    if not daily_basic:
        return {"score": 50, "drivers": ["无估值数据"], "label": "估值", "weight": 25}

    pe = daily_basic.get("pe", 0) or 0
    pb = daily_basic.get("pb", 0) or 0

    # PE 评分：0-15 低估分高, 15-30 合理, >30 偏高, <0 亏损
    if pe <= 0:
        score -= 15
        drivers.append(f"PE亏损")
    elif pe < 15:
        score += 20
        drivers.append(f"PE={pe:.1f}(低估)")
    elif pe < 30:
        score += 5
        drivers.append(f"PE={pe:.1f}(合理)")
    elif pe < 60:
        score -= 10
        drivers.append(f"PE={pe:.1f}(偏高)")
    else:
        score -= 20
        drivers.append(f"PE={pe:.1f}(过高)")

    # PB 评分：<1 破净, 1-3 合理, >5 偏高
    if pb <= 0:
        pass
    elif pb < 1:
        score += 15
        drivers.append(f"PB={pb:.2f}(破净)")
    elif pb < 3:
        score += 5
        drivers.append(f"PB={pb:.2f}(合理)")
    elif pb < 5:
        score -= 5
        drivers.append(f"PB={pb:.2f}(偏高)")
    else:
        score -= 10
        drivers.append(f"PB={pb:.2f}(过高)")

    score = max(1, min(99, round(score)))
    return {"score": score, "drivers": drivers, "label": "估值", "weight": 25}


def _score_quality(financial: dict | None) -> dict:
    """质量维度评分(0-100): 盈利性+成长性+偿债能力。"""
    drivers: list[str] = []
    score = 50.0

    if not financial:
        return {"score": 50, "drivers": ["无财务数据"], "label": "质量", "weight": 25}

    roe = _safe_float(financial.get("roe"))
    roa = _safe_float(financial.get("roa"))
    gp_margin = _safe_float(financial.get("grossprofit_margin"))
    np_margin = _safe_float(financial.get("netprofit_margin"))
    rev_yoy = _safe_float(financial.get("or_yoy"))
    profit_yoy = _safe_float(financial.get("profit_dedt"))
    debt_ratio = _safe_float(financial.get("debt_to_assets"))

    # ROE
    if roe > 20:
        score += 15; drivers.append(f"ROE={roe:.1f}%(优秀)")
    elif roe > 10:
        score += 8; drivers.append(f"ROE={roe:.1f}%(良好)")
    elif roe > 5:
        score += 2; drivers.append(f"ROE={roe:.1f}%(一般)")
    elif roe < 0:
        score -= 10; drivers.append(f"ROE={roe:.1f}%(亏损)")

    # 净利率
    if np_margin > 20:
        score += 10; drivers.append(f"净利率={np_margin:.1f}%(优秀)")
    elif np_margin > 10:
        score += 5; drivers.append(f"净利率={np_margin:.1f}%(良好)")
    elif np_margin <= 0:
        score -= 8; drivers.append(f"净利率={np_margin:.1f}%(亏损)")

    # 营收增速
    if rev_yoy > 30:
        score += 12; drivers.append(f"营收增速={rev_yoy:.1f}%(高增)")
    elif rev_yoy > 10:
        score += 6; drivers.append(f"营收增速={rev_yoy:.1f}%(增长)")
    elif rev_yoy < -10:
        score -= 10; drivers.append(f"营收增速={rev_yoy:.1f}%(下滑)")

    # 扣非净利增速（profit_dedt 可能是绝对值万元，超过 500 视为非增速，跳过）
    if 0 < profit_yoy < 500:
        if profit_yoy > 30:
            score += 10; drivers.append(f"净利增速={profit_yoy:.1f}%(高增)")
        elif profit_yoy < -20:
            score -= 10; drivers.append(f"净利增速={profit_yoy:.1f}%(下滑)")

    # 资产负债率 (40-60% 合理)
    if 40 <= debt_ratio <= 60:
        pass  # neutral
    elif debt_ratio > 80:
        score -= 8; drivers.append(f"负债率={debt_ratio:.1f}%(偏高)")
    elif debt_ratio > 60:
        score -= 3; drivers.append(f"负债率={debt_ratio:.1f}%(略高)")

    score = max(1, min(99, round(score)))
    return {"score": score, "drivers": drivers, "label": "质量", "weight": 25}


def _score_momentum(closes, highs, lows, volumes) -> dict:
    """动量维度评分(0-100): 趋势+超买超卖+成交量。沿用原 _calc_quant_score 逻辑但独立评分。"""
    from app.services.factor_lib import sma as _sma_m, macd as _macd_m, rsi as _rsi_m
    from app.services.factor_lib import kdj as _kdj_m, bollinger as _boll_m

    drivers: list[str] = []
    macd_d = _macd_m(closes)
    rsi_v = _rsi_m(closes)
    kdj_d = _kdj_m(highs, lows, closes)
    boll_d = _boll_m(closes)

    last = len(closes) - 1
    dif = macd_d["dif"][last] or 0
    dea = macd_d["dea"][last] or 0
    bar = macd_d["bar"][last] or 0
    rsi_now = rsi_v[-1] if rsi_v else 50
    k_now = kdj_d["k"][-1] if kdj_d["k"] else 50
    d_now = kdj_d["d"][-1] if kdj_d["d"] else 50
    j_now = kdj_d["j"][-1] if kdj_d["j"] else 50
    close = closes[-1]
    boll_lower = boll_d["lower"][-1] or close
    boll_upper = boll_d["upper"][-1] or close
    boll_pos = (close - boll_lower) / (boll_upper - boll_lower) if boll_upper != boll_lower else 0.5
    vol_ratio = volumes[-1] / (sum(volumes[-20:]) / 20) if len(volumes) >= 20 and sum(volumes[-20:]) > 0 else 1

    score = 50.0

    # RSI
    if rsi_now > 80: score -= 15; drivers.append(f"RSI={rsi_now:.0f}(超买)")
    elif rsi_now > 70: score -= 5; drivers.append(f"RSI={rsi_now:.0f}(偏买)")
    elif rsi_now < 20: score += 15; drivers.append(f"RSI={rsi_now:.0f}(超卖)")
    elif rsi_now < 30: score += 5; drivers.append(f"RSI={rsi_now:.0f}(偏卖)")

    # KDJ
    if j_now > 100: score -= 5; drivers.append("KDJ超买")
    elif j_now < 0: score += 10; drivers.append("KDJ超卖")
    elif k_now > d_now and j_now > 50: score += 5; drivers.append("KDJ金叉区域")

    # MACD
    if dif > dea and bar > 0: score += 8; drivers.append("MACD多头")
    elif dif < dea and bar < 0: score -= 8; drivers.append("MACD空头")
    elif dif > dea: score += 3; drivers.append("MACD偏多")
    elif dif < dea: score -= 3; drivers.append("MACD偏空")

    # 布林
    if boll_pos > 0.9: score -= 5; drivers.append("触及上轨")
    elif boll_pos < 0.1: score += 10; drivers.append("触及下轨")

    # 量
    if vol_ratio > 2: score += 5; drivers.append(f"放量{vol_ratio:.1f}x")
    elif vol_ratio < 0.5: score -= 3; drivers.append(f"缩量{vol_ratio:.1f}x")

    score = max(1, min(99, round(score)))
    return {"score": score, "drivers": drivers, "label": "动量", "weight": 25}


def _score_sentiment(holders: dict | None, margin: dict | None) -> dict:
    """情绪维度评分(0-100): 筹码集中度 + 融资态度。"""
    drivers: list[str] = []
    score = 50.0

    # 股东人数趋势：集中=看多
    if holders and holders.get("trend") == "concentrated":
        score += 15
        drivers.append("筹码集中")
    elif holders and holders.get("trend") == "dispersed":
        score -= 10
        drivers.append("筹码分散")
    elif not holders:
        drivers.append("无股东数据")

    # 融资余额变化倾向（连续买入看多）
    if margin:
        rzye = _safe_float(margin.get("rzye"))
        rzmre = _safe_float(margin.get("rzmre"))
        if rzmre > rzye * 0.05:
            score += 10
            drivers.append("融资大幅买入")
        elif rzmre > 0:
            score += 3
            drivers.append("融资净买入")
    else:
        drivers.append("无融资数据")

    score = max(1, min(99, round(score)))
    return {"score": score, "drivers": drivers, "label": "情绪", "weight": 25}


def _score_dimensions(basic, financial, closes, highs, lows, volumes, holders, margin) -> dict:
    """四维度评分 → 加权综合。"""
    dims = [
        _score_valuation(basic),
        _score_quality(financial),
        _score_momentum(closes, highs, lows, volumes),
        _score_sentiment(holders, margin),
    ]

    total = sum(d["score"] * d["weight"] for d in dims) / 100
    total = round(max(1, min(99, total)))

    if total >= 75: risk = "低风险"
    elif total >= 60: risk = "中低风险"
    elif total >= 40: risk = "中风险"
    elif total >= 25: risk = "中高风险"
    else: risk = "高风险"

    return {"total": total, "risk": risk, "dimensions": dims}


def _calc_compat_score(rsi, k, d, j, dif, dea, bar, boll_pos, vol_ratio):
    """兼容旧版动量评分——作为 _compute_diagnosis 内的初始评分，
    后续被 _score_dimensions 四维度评分的 total/risk 覆盖。"""
    score = 50
    if rsi > 80: score -= 15
    elif rsi > 70: score -= 5
    elif rsi < 20: score += 15
    elif rsi < 30: score += 5
    if j > 100: score -= 5
    elif j < 0: score += 10
    elif k > d and j > 50: score += 5
    if dif > dea and bar > 0: score += 8
    elif dif < dea and bar < 0: score -= 8
    elif dif > dea: score += 3
    elif dif < dea: score -= 3
    if boll_pos > 0.9: score -= 5
    elif boll_pos < 0.1: score += 10
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

def _build_kline_data(records: list[dict]) -> dict:
    """将list[dict]日线数据转为前端ECharts可渲染的K线JSON"""
    kline = []
    for r in records:
        kline.append([
            str(r.get("trade_date", "")),
            round(float(r.get("open", 0) or 0), 2),
            round(float(r.get("close", 0) or 0), 2),
            round(float(r.get("low", 0) or 0), 2),
            round(float(r.get("high", 0) or 0), 2),
            round(float(r.get("vol", 0) or 0), 0),
        ])
    kline.reverse()  # 最旧在前，ECharts candlestick 格式
    return {"columns": ["trade_date", "open", "close", "low", "high", "volume"], "rows": kline}


def _build_indicator_series(closes, highs, lows) -> dict:
    """返回完整指标时间序列，供前端多副图渲染。"""
    n = len(closes)

    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    boll = _bollinger(closes)
    macd = _macd(closes)
    kdj = _kdj(highs, lows, closes)
    rsi_vals = _rsi(closes)

    # 各指标起始偏移（前面多少天为 None）
    kdj_offset = n - len(kdj["k"])
    rsi_offset = n - len(rsi_vals)

    def _r(v, idx):
        return None if v is None else round(v, 2)

    series = []
    for i in range(n):
        item = {
            "ma5": _r(ma5[i], i),
            "ma10": _r(ma10[i], i),
            "ma20": _r(ma20[i], i),
            "boll_upper": _r(boll["upper"][i], i),
            "boll_mid": _r(boll["mid"][i], i),
            "boll_lower": _r(boll["lower"][i], i),
            "macd_dif": _r(macd["dif"][i], i),
            "macd_dea": _r(macd["dea"][i], i),
            "macd_bar": _r(macd["bar"][i], i),
            "kdj_k": _r(kdj["k"][i - kdj_offset], i) if i >= kdj_offset else None,
            "kdj_d": _r(kdj["d"][i - kdj_offset], i) if i >= kdj_offset else None,
            "kdj_j": _r(kdj["j"][i - kdj_offset], i) if i >= kdj_offset else None,
            "rsi": _r(rsi_vals[i - rsi_offset], i) if i >= rsi_offset else None,
        }
        series.append(item)

    return {"length": n, "series": series}


# ── API ──

@router.get("/quota")
async def get_quota(request: Request, user: dict = Depends(require_auth_optional)):
    """返回当前用户积分余额和诊股消耗规则。"""
    if user:
        tier = user["tier"]
        user_id = user["user_id"]
    else:
        tier = 0
        user_id = 0

    if tier >= 2:
        return APIResponse(
            data={
                "cost": 0,
                "tier": tier,
                "require_login": False,
                "rule": "VIP免费诊股",
            },
            timestamp=int(time.time()),
        )

    if tier == 0:
        # 取消游客门禁：游客免费诊股
        return APIResponse(
            data={"cost": 0, "tier": 0, "require_login": False, "rule": "游客免费诊股"},
            timestamp=int(time.time()),
        )

    # tier=1 注册用户：1积分/次
    async with async_session() as session:
        result = await session.execute(select(User.credits).where(User.id == user_id))
        credits = result.scalar() or 0

    return APIResponse(
        data={
            "cost": 1,
            "tier": tier,
            "credits": credits,
            "require_login": False,
            "rule": "1积分/次诊股",
        },
        timestamp=int(time.time()),
    )


@router.get("/moneyflow")
async def get_stock_moneyflow(stock_code: str, user: dict = Depends(require_auth_optional)):
    """个股资金流向——主力净流入趋势 + 四单分布。"""
    cache_key = f"diag_mf:{stock_code}"
    cached = await cache_get(cache_key)
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    from app.services.tushare_client import get_moneyflow

    code = stock_code
    if "." not in code:
        for suffix in [".SZ", ".SH"]:
            code = stock_code + suffix
            break

    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    try:
        rows = await get_moneyflow(code, start, end)
    except Exception as e:
        return APIResponse(
            data={"stock_code": stock_code, "trend": [], "summary": None},
            timestamp=int(time.time()),
            ext_info={"note": f"Tushare 调用失败: {e}"}
        )

    if not rows:
        return APIResponse(
            data={"stock_code": stock_code, "trend": [], "summary": None},
            timestamp=int(time.time()),
            ext_info={"note": "暂无资金流向数据"}
        )

    sorted_rows = sorted(rows, key=lambda r: r.get("trade_date", ""))
    trend = []
    for r in sorted_rows:
        # Tushare moneyflow 返回万元，转为元
        trend.append({
            "date": r.get("trade_date", ""),
            "net_mf_amount": round(float(r.get("net_mf_amount", 0) or 0) * 1e4, 2),
            "buy_elg_amount": round(float(r.get("buy_elg_amount", 0) or 0) * 1e4, 2),
            "sell_elg_amount": round(float(r.get("sell_elg_amount", 0) or 0) * 1e4, 2),
            "buy_lg_amount": round(float(r.get("buy_lg_amount", 0) or 0) * 1e4, 2),
            "sell_lg_amount": round(float(r.get("sell_lg_amount", 0) or 0) * 1e4, 2),
            "buy_md_amount": round(float(r.get("buy_md_amount", 0) or 0) * 1e4, 2),
            "sell_md_amount": round(float(r.get("sell_md_amount", 0) or 0) * 1e4, 2),
            "buy_sm_amount": round(float(r.get("buy_sm_amount", 0) or 0) * 1e4, 2),
            "sell_sm_amount": round(float(r.get("sell_sm_amount", 0) or 0) * 1e4, 2),
        })

    total_net_mf = sum(t["net_mf_amount"] for t in trend)
    total_elg = sum(t["buy_elg_amount"] - t["sell_elg_amount"] for t in trend)
    total_lg = sum(t["buy_lg_amount"] - t["sell_lg_amount"] for t in trend)
    total_md = sum(t["buy_md_amount"] - t["sell_md_amount"] for t in trend)
    total_sm = sum(t["buy_sm_amount"] - t["sell_sm_amount"] for t in trend)

    summary = {
        "net_mf_amount": round(total_net_mf, 2),
        "net_elg_amount": round(total_elg, 2),
        "net_lg_amount": round(total_lg, 2),
        "net_md_amount": round(total_md, 2),
        "net_sm_amount": round(total_sm, 2),
        "days": len(trend),
    }

    data = {"stock_code": stock_code, "trend": trend, "summary": summary}
    await cache_set(cache_key, data, ttl=_settings.cache_diagnosis_ttl)
    return APIResponse(data=data, timestamp=int(time.time()))


@router.get("/{stock_code}")
async def get_diagnosis(stock_code: str, request: Request, user: dict = Depends(require_auth_optional)):
    if user:
        tier = user["tier"]
        user_id = user["user_id"]
    else:
        tier = 0
        user_id = 0

    # 取消游客门禁：游客(tier=0)可直接诊股不扣分，注册用户扣1分，VIP免费
    today = date.today().isoformat()
    cache_key = f"diag_v2:{stock_code}"
    cached = await cache_get(cache_key)
    if cached:
        return _build_response(cached, tier, cache_hit=True, cache_date=today)

    # 积分扣减：仅注册用户(tier=1)扣1分，游客/VIP不扣
    if 0 < tier < 2:
        async with async_session() as session:
            u_result = await session.execute(select(User).where(User.id == user_id))
            u = u_result.scalar_one_or_none()
            if not u:
                raise HTTPException(status_code=404, detail="用户不存在")
            if (u.credits or 0) < 1:
                return APIResponse(
                    code=403,
                    message="积分不足，每次诊股消耗1积分。请签到或升级VIP获取免费诊股",
                    data={"credits": u.credits, "cost": 1},
                    timestamp=int(time.time()),
                ).model_dump()

            u.credits = u.credits - 1
            session.add(CreditLedger(
                user_id=user_id,
                amount=-1,
                type="diagnosis",
                ref_id=stock_code,
                balance_after=u.credits,
                note=f"诊股 {stock_code}",
            ))
            await session.commit()

    report = await _compute_diagnosis(stock_code)
    if report is None:
        raise HTTPException(status_code=404, detail=f"股票 {stock_code} 暂无数据")

    report["financial"] = await _fetch_financial_snapshot(stock_code)
    report["holders"] = await _fetch_holder_snapshot(stock_code)
    report["margin"] = await _fetch_margin_snapshot(stock_code)

    # 解析实际 ts_code 用于 daily_basic 查询
    final_code = stock_code
    if "." not in final_code:
        async with async_session() as _ds:
            for suffix in [".SH", ".SZ"]:
                _rs = await _ds.execute(text("SELECT 1 FROM stocks WHERE ts_code=:c LIMIT 1"), {"c": stock_code + suffix})
                if _rs.first():
                    final_code = stock_code + suffix
                    break
    report["daily_basic"] = await _fetch_daily_basic_snapshot(final_code)

    # 四维度评分覆盖初始动量分
    q = report.get("quant", {})
    if q:
        dims = _score_dimensions(
            report.get("daily_basic"),
            report.get("financial"),
            report.get("_closes", []),
            report.get("_highs", []),
            report.get("_lows", []),
            report.get("_volumes", []),
            report.get("holders"),
            report.get("margin"),
        )
        q["score"] = dims["total"]
        q["risk"] = dims["risk"]
        q["dimensions"] = dims["dimensions"]
        # 更新建议
        q["suggestion"] = _gen_suggestion(dims["total"], dims["risk"], q.get("close", 0), q.get("key_levels", {}))
        # 清理内部传递的序列
        for _k in ("_closes", "_highs", "_lows", "_volumes"):
            q.pop(_k, None)
        report.pop("_closes", None)
        report.pop("_highs", None)
        report.pop("_lows", None)
        report.pop("_volumes", None)

    await cache_set(cache_key, report, ttl=_settings.cache_diagnosis_ttl)
    return _build_response(report, tier, cache_hit=False, cache_date=today)


async def _compute_diagnosis(stock_code: str) -> dict | None:
    """从Tushare获取60天日线数据 → 计算技术指标 → 生成报告"""
    from app.services.tushare_client import get_daily_data

    try:
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=120)).strftime("%Y%m%d")

        records = None
        for suffix in [".SZ", ".SH", ""]:
            code = stock_code if suffix == "" else (stock_code if "." in stock_code else stock_code + suffix)
            records = await get_daily_data(code, start, end)
            if records:
                break

        if not records:
            return None

        # records is list[dict] from get_daily_data
        closes = [float(r["close"]) for r in records if r.get("close") and float(r.get("close", 0)) > 0]
        highs  = [float(r["high"]) for r in records if r.get("high")]
        lows   = [float(r["low"]) for r in records if r.get("low")]
        opens  = [float(r["open"]) for r in records if r.get("open")]
        vols   = [float(r["vol"]) for r in records if r.get("vol")]

        if len(closes) < 20:
            return None

        closes.reverse(); highs.reverse(); lows.reverse(); opens.reverse(); vols.reverse()

        quant = _quant_signal(closes, highs, lows, vols)
        kline = _build_kline_data(records)
        indicators = _build_indicator_series(closes, highs, lows)

        # 获取股票名称（DB查stocks表，不调Tushare下载4600+条）
        stock_name = stock_code
        try:
            from app.core.database import async_session as _diag_sess
            from sqlalchemy import text as _text
            async with _diag_sess() as _s:
                _r = await _s.execute(_text("SELECT name FROM stocks WHERE ts_code=:c"), {"c": code})
                _row = _r.first()
                if _row:
                    stock_name = _row[0]
        except Exception:
            pass

        # 因子诊断（基于有效因子库，配置驱动——因子库更新后自动用新因子集）
        factor_diag = None
        try:
            from app.services.factor_engine import diagnose_all
            factor_diag = await diagnose_all(code)
        except Exception:
            pass

        # 风险信号扫描（放量见顶/龙虎榜上榜，独立于选股因子）
        risks = []
        try:
            from app.services.factor_engine import scan_risks
            risks = await scan_risks(code)
        except Exception:
            pass

        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "quant": quant,
            "factor_diag": factor_diag,
            "risks": risks,
            # 四维度评分重建所需原始序列
            "_closes": closes,
            "_highs": highs,
            "_lows": lows,
            "_volumes": vols,
            "kline": kline,
            "indicators": indicators,
            "financial": None,
        }
    except Exception as e:
        import logging
        logging.getLogger("diagnosis").exception(f"_compute_diagnosis({stock_code}) failed: {e}")
        return None


async def _fetch_daily_basic_snapshot(code: str) -> dict | None:
    """从 daily_basic 表读取最新估值指标（PE/PB/总市值），当日缓存。"""
    today = date.today().isoformat()
    cache_key = f"basic:{code}:{today}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached if cached else None

    from sqlalchemy import text as _text
    row = None
    async with async_session() as sess:
        for offset in range(7):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            r = await sess.execute(
                _text("SELECT pe, pb, total_mv, turnover_rate FROM daily_basic "
                      "WHERE ts_code = :ts AND trade_date = :td"),
                {"ts": code, "td": td},
            )
            row = r.first()
            if row:
                break

    if not row:
        await cache_set(cache_key, {}, ttl=86400)
        return None

    result = {
        "pe": _safe_float(row[0]),
        "pb": _safe_float(row[1]),
        "total_mv": _safe_float(row[2]),      # 万元
        "turnover_rate": _safe_float(row[3]),
    }
    await cache_set(cache_key, result, ttl=86400)
    return result


async def _fetch_financial_snapshot(stock_code: str) -> dict | None:
    """按代码+日期缓存财务指标，当天内不重复请求Tushare。"""
    today = date.today().isoformat()
    cache_key = f"fina:{stock_code}:{today}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached if cached else None

    from app.services.tushare_client import get_fina_indicator

    code = stock_code
    if "." not in code:
        code = stock_code + (".SH" if stock_code.startswith(("5", "6", "9")) else ".SZ")

    result = await get_fina_indicator(code)
    await cache_set(cache_key, result or {}, ttl=86400)
    return result


def _safe_float(val, default=0.0):
    """Safely convert a value to float, handling NaN and None."""
    import math
    if val is None:
        return default
    f = float(val)
    return default if math.isnan(f) else f


async def _fetch_holder_snapshot(stock_code: str) -> dict | None:
    """股东人数变化——当天内缓存不重复请求。"""
    today = date.today().isoformat()
    cache_key = f"holder:{stock_code}:{today}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached if cached else None

    from app.services.tushare_client import get_stk_holdernumber

    code = stock_code
    if "." not in code:
        code = stock_code + (".SH" if stock_code.startswith(("5", "6", "9")) else ".SZ")

    try:
        rows = await get_stk_holdernumber(code)
    except Exception:
        rows = []

    if not rows:
        await cache_set(cache_key, {}, ttl=86400)
        return None

    recent = sorted(rows, key=lambda r: r.get("end_date", ""), reverse=True)[:3]
    result = {
        "holders": [{
            "end_date": str(r.get("end_date", "")),
            "holder_num": int(_safe_float(r.get("holder_num"), 0)),
            "top_holder_ratio": round(_safe_float(r.get("top_holder_ratio"), 0), 2),
        } for r in recent],
        "trend": "concentrated" if (
            len(recent) >= 2 and recent[0].get("holder_num", 0) < recent[1].get("holder_num", 0)
        ) else "dispersed",
    }
    await cache_set(cache_key, result, ttl=86400)
    return result


async def _fetch_margin_snapshot(stock_code: str) -> dict | None:
    """从 margin_records 表读取最新融资融券数据。"""
    from sqlalchemy import text

    code = stock_code
    if "." not in code:
        code = stock_code + (".SH" if stock_code.startswith(("5", "6", "9")) else ".SZ")

    async with async_session() as session:
        r = await session.execute(
            text("SELECT rzye, rqye, rzmre, rzrqye FROM margin_records "
                 "WHERE ts_code = :ts ORDER BY trade_date DESC LIMIT 1"),
            {"ts": code},
        )
        row = r.first()
        if not row:
            return None
        return {
            "rzye": round(float(row[0] or 0), 2),
            "rqye": round(float(row[1] or 0), 2),
            "rzmre": round(float(row[2] or 0), 2),
            "rzrqye": round(float(row[3] or 0), 2),
        }


def _build_response(report: dict, tier: int, cache_hit: bool, cache_date: str = "") -> APIResponse:
    data = {
        "stock_code": report["stock_code"],
        "stock_name": report["stock_name"],
        "quant": report["quant"],
        "factor_diag": report.get("factor_diag"),
        "risks": report.get("risks", []),
        "kline": report.get("kline"),
        "indicators": report.get("indicators"),
        "financial": report.get("financial"),
        "holders": report.get("holders"),
        "margin": report.get("margin"),
        "daily_basic": report.get("daily_basic"),
    }

    ext = {"cache_hit": cache_hit}
    if cache_hit and cache_date:
        ext["cache_date"] = cache_date
        ext["cache_note"] = "数据缓存于交易日，24小时内有效，建议下载报告保存"

    return APIResponse(
        data=data,
        timestamp=int(time.time()),
        ext_info=ext,
    )


# ── AI 分析 ──


async def _load_turnover_rates(ts_code: str, daily: list[dict]) -> list[float] | None:
    """从 daily_basic 加载与 daily 对齐的换手率序列（缺失为 0.0）。"""
    from sqlalchemy import text as _text
    async with async_session() as _s:
        _r = await _s.execute(
            _text("SELECT trade_date, turnover_rate FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"),
            {"c": ts_code},
        )
        rows = _r.mappings().all()
    turnover_map = {str(row["trade_date"]): float(row["turnover_rate"] or 0) for row in rows}
    return [turnover_map.get(d["trade_date"], 0.0) for d in daily]


async def _five_eye_reference(stock_code: str) -> dict | None:
    """加载日线 → 跑五眼共识 → 返回供 AI 辩论参考的紧凑结论。失败返回 None。

    与既有 *_test.py 用同一数据源/格式，保证"参考背景"与已验证结论同口径。
    """
    from app.services.calibration import _load_daily_data_fast
    from app.services.multi_eye import consensus as _run_consensus

    ts_code = stock_code
    if "." not in ts_code:
        from sqlalchemy import text as _text
        async with async_session() as _s:
            _r = await _s.execute(
                _text("SELECT ts_code FROM stocks WHERE ts_code LIKE :p ORDER BY ts_code LIMIT 1"),
                {"p": ts_code + ".%"},
            )
            _row = _r.first()
            if _row:
                ts_code = _row[0]

    try:
        daily = await _load_daily_data_fast(ts_code)
        if not daily or len(daily) < 30:
            return None
        turnover_rates = await _load_turnover_rates(ts_code, daily)
        cons = _run_consensus(daily, turnover_rates)
        return {
            "five_eye_summary": cons.summary,
            "trend": cons.trend.get("verdict"),
            "position": cons.position.get("verdict"),
            "signal": cons.signal.get("verdict"),
            "retreat_alert": cons.retreat_alert,
        }
    except Exception:
        return None


@router.post("/{stock_code}/ai-analysis")
async def ai_analysis(stock_code: str, mode: str = "single", user: dict = Depends(require_auth)):
    """AI 辅助解读——调用 DeepSeek 分析技术指标，扣2积分/次，同股同日缓存命中不扣。

    mode: single=单Agent解读(默认) / debate=多空辩论+结构化评级(原型)
    """
    if mode not in ("single", "debate"):
        mode = "single"
    user_id = user["user_id"]
    today = date.today().isoformat()
    ai_cache_key = f"ai_debate:{stock_code}:{today}" if mode == "debate" else f"ai:{stock_code}:{today}"

    cached = await cache_get(ai_cache_key)
    cache_hit = cached is not None
    result = None

    if not cache_hit:
        # 先做诊股计算获取指标数据
        diag_cache_key = f"diag_v2:{stock_code}"
        diag_cached = await cache_get(diag_cache_key)
        if diag_cached:
            report = diag_cached
        else:
            report = await _compute_diagnosis(stock_code)
            if report is None:
                raise HTTPException(status_code=404, detail=f"股票 {stock_code} 暂无数据，请先诊股")

        indicators = report.get("quant", {})

        # 扣积分 (2分/次，全用户统一)
        async with async_session() as session:
            u_result = await session.execute(select(User).where(User.id == user_id))
            u = u_result.scalar_one_or_none()
            if not u:
                raise HTTPException(status_code=404, detail="用户不存在")
            if (u.credits or 0) < _settings.ai_analysis_cost:
                return APIResponse(
                    code=403,
                    message=f"积分不足，AI分析消耗{_settings.ai_analysis_cost}积分，当前余额：{u.credits}",
                    data={"credits": u.credits, "cost": _settings.ai_analysis_cost},
                    timestamp=int(time.time()),
                ).model_dump()

            u.credits = u.credits - _settings.ai_analysis_cost
            session.add(CreditLedger(
                user_id=user_id,
                amount=-_settings.ai_analysis_cost,
                type="ai_analysis",
                ref_id=stock_code,
                balance_after=u.credits,
                note=f"AI分析 {stock_code}",
            ))
            await session.commit()

        # 调用 LLM
        from app.services.ai_analysis import analyze_stock, analyze_stock_debate
        try:
            if mode == "debate":
                extra_evidence = await _five_eye_reference(stock_code)
                result = await analyze_stock_debate(
                    stock_code=stock_code,
                    stock_name=report.get("stock_name", stock_code),
                    indicators_json=indicators,
                    extra_evidence=extra_evidence,
                )
                text = result["report"]
            else:
                text = await analyze_stock(
                    stock_code=stock_code,
                    stock_name=report.get("stock_name", stock_code),
                    indicators_json=indicators,
                )
        except Exception as e:
            # LLM 调用失败，退还积分
            async with async_session() as session:
                u_result2 = await session.execute(select(User).where(User.id == user_id))
                u2 = u_result2.scalar_one_or_none()
                if u2:
                    u2.credits = (u2.credits or 0) + _settings.ai_analysis_cost
                    session.add(CreditLedger(
                        user_id=user_id,
                        amount=_settings.ai_analysis_cost,
                        type="admin",
                        ref_id=stock_code,
                        balance_after=u2.credits,
                        note=f"AI分析失败退分 {stock_code}",
                    ))
                    await session.commit()
            raise HTTPException(status_code=502, detail=f"AI服务暂不可用: {e}")
    else:
        if mode == "debate" and isinstance(cached, dict):
            result = cached
            text = cached.get("report", "")
        else:
            text = cached

    data = {
        "stock_code": stock_code,
        "analysis": text,
        "cache_hit": cache_hit,
        "cost": _settings.ai_analysis_cost,
    }
    if mode == "debate" and isinstance(result, dict):
        data["rating"] = result.get("rating")
        data["mode"] = "debate"

    return APIResponse(
        data=data,
        timestamp=int(time.time()),
    )
