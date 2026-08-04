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

    # 游客必须先登录
    if tier == 0:
        return APIResponse(
            data={"cost": 1, "tier": tier, "require_login": True},
            timestamp=int(time.time()),
        )

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

    # 游客必须先登录
    if tier == 0:
        return APIResponse(
            code=403,
            message="请先登录后再使用诊股功能",
            data={"require_login": True},
            timestamp=int(time.time()),
        ).model_dump()

    today = date.today().isoformat()
    cache_key = f"diag_v2:{stock_code}"
    cached = await cache_get(cache_key)
    if cached:
        return _build_response(cached, tier, cache_hit=True, cache_date=today)

    # 积分扣减：VIP免费，注册用户扣1分
    if tier < 2:
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
            "indicators": indicators,
            "financial": None,  # _enhance_financials() 单独填充
        }
    except Exception as e:
        import logging
        logging.getLogger("diagnosis").exception(f"_compute_diagnosis({stock_code}) failed: {e}")
        return None


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
        "kline": report.get("kline"),
        "indicators": report.get("indicators"),
        "financial": report.get("financial"),
        "holders": report.get("holders"),
        "margin": report.get("margin"),
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


@router.post("/{stock_code}/ai-analysis")
async def ai_analysis(stock_code: str, user: dict = Depends(require_auth)):
    """AI 辅助解读——调用 DeepSeek 分析技术指标，扣2积分/次，同股同日缓存命中不扣。"""
    user_id = user["user_id"]
    today = date.today().isoformat()
    ai_cache_key = f"ai:{stock_code}:{today}"

    cached = await cache_get(ai_cache_key)
    cache_hit = cached is not None

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
        from app.services.ai_analysis import analyze_stock
        try:
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
        text = cached

    return APIResponse(
        data={
            "stock_code": stock_code,
            "analysis": text,
            "cache_hit": cache_hit,
        },
        timestamp=int(time.time()),
    )
