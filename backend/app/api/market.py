"""市场行情 API——板块分析、每日复盘、风险避雷、指数行情、市场情绪。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException

from app.core.cache import cache_get
from app.models.schemas.common import APIResponse

sector_router = APIRouter(prefix="/api/v1/sector-rotation", tags=["板块轮动"])
review_router = APIRouter(prefix="/api/v1/review", tags=["每日复盘"])
risk_router = APIRouter(prefix="/api/v1/risk-list", tags=["风险避雷"])

# market router for general market endpoints
market_router = APIRouter(prefix="/api/v1/market", tags=["市场概览"])

from app.api.akshare_client import safe_akshare_call, is_trading_time, get_cache_ttl

INDEX_CODES = [
    ("000001.SH", "上证指数", "000001"),
    ("399001.SZ", "深证成指", "399001"),
    ("399006.SZ", "创业板指", "399006"),
    ("000688.SH", "科创50", "000688"),
]


def _is_trading_time() -> bool:
    return is_trading_time()


@market_router.get("/stock_count")
async def stock_count():
    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    cached = await cache_get("market:stock_count")
    if cached is not None:
        return APIResponse(data={"count": cached}, timestamp=int(time.time()))

    async with async_session() as sess:
        r = await sess.execute(text("SELECT COUNT(*) FROM stocks"))
        count = r.scalar() or 0

    await cache_set("market:stock_count", count, ttl=7200)
    return APIResponse(data={"count": count}, timestamp=int(time.time()))


def _parse_index_from_akshare(data: list) -> list[dict]:
    """从 AKShare stock_zh_index_spot_em 结果解析4大指数。行情数据列: 代码,名称,最新价,涨跌幅(%),涨跌额,成交量,成交额,今开,昨收,最高,最低"""
    if not data:
        return []
    code_map = {c[2]: c for c in INDEX_CODES}  # akshare code → (ts_code, name)
    results = []
    for row in data:
        if hasattr(row, "to_dict"):
            row = row.to_dict()
        elif isinstance(row, (list, tuple)):
            row = {"code": row[0], "name": row[1], "close": row[2], "pct_chg": row[3], "change": row[4]}
        akshare_code = str(row.get("code", ""))
        if akshare_code not in code_map:
            continue
        ts_code, name, _ = code_map[akshare_code]
        try:
            results.append({
                "code": ts_code,
                "name": name,
                "close": round(float(row.get("close", 0) or row.get("latest", 0) or 0), 2),
                "pct_chg": round(float(row.get("pct_chg", 0) or 0), 2),
                "change": round(float(row.get("change", 0) or 0), 2),
            })
        except (ValueError, TypeError):
            continue
        code_order = {c[0]: i for i, c in enumerate(INDEX_CODES)}
        results.sort(key=lambda r: code_order.get(r["code"], 99))
        return results


def _parse_mood_from_akshare(data: list) -> dict:
    """从 AKShare stock_zh_a_spot_em 统计涨跌平家数和涨跌停数。列: 代码,名称,最新价,涨跌幅,涨跌额,成交量,成交额,今开,昨收,最高,最低,..."""
    if not data:
        return {}
    up = down = flat = limit_up = limit_down = 0
    for row in data:
        try:
            d = row.to_dict() if hasattr(row, "to_dict") else (dict(zip(["code","name","close","pct_chg","change","vol","amount","open","pre_close","high","low"], row)) if isinstance(row, (list, tuple)) else row)
            pct = float(d.get("pct_chg", 0) or 0)
            if pct > 0:
                up += 1
                if pct >= 9.9:
                    limit_up += 1
            elif pct < 0:
                down += 1
                if pct <= -9.9:
                    limit_down += 1
            else:
                flat += 1
        except (ValueError, TypeError):
            pass
    return {"up": up, "down": down, "flat": flat, "limit_up": limit_up, "limit_down": limit_down}


@market_router.get("/index")
async def market_index():
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    # 1. Try AKShare (primary)
    try:
        raw = await safe_akshare_call("stock_zh_index_spot_em", cache_key="index_realtime")
        if raw is not None:
            indices = _parse_index_from_akshare(raw)
            if len(indices) >= 2:
                return APIResponse(data={
                    "indices": indices,
                    "source": "akshare_realtime",
                    "update_time": now_ts,
                    "trading": trading,
                }, timestamp=int(time.time()))
    except Exception:
        pass

    # 2. Fallback: SQLite cache → Tushare
    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    today_key = date.today().strftime("%Y%m%d")
    cache_key = f"market:index:{today_key}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return APIResponse(data={"indices": cached, "source": "tushare_daily", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))

    results = []
    async with async_session() as sess:
        for code, name, _ in INDEX_CODES:
            r = await sess.execute(
                text("SELECT close, pct_chg, change FROM stock_daily WHERE ts_code=:code ORDER BY trade_date DESC LIMIT 1"),
                {"code": code},
            )
            row = r.first()
            if row:
                results.append({"code": code, "name": name, "close": round(row[0], 2), "pct_chg": round(row[1], 2), "change": round(row[2], 2)})

    if results and all(r["close"] for r in results):
        ttl = 300 if trading else 3600
        await cache_set(cache_key, results, ttl=ttl)
        return APIResponse(data={"indices": results, "source": "tushare_daily", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))

    # 3. Final fallback: Tushare API
    try:
        from app.services.tushare_client import call_tushare
        import pandas as pd
        results = []
        for code, name, _ in INDEX_CODES:
            df = await call_tushare("index_daily", ts_code=code, start_date=(date.today() - timedelta(days=7)).strftime("%Y%m%d"))
            if df is not None and not (hasattr(df, "empty") and df.empty):
                df_sorted = df.sort_values("trade_date", ascending=False)
                latest = df_sorted.iloc[0]
                results.append({
                    "code": code, "name": name,
                    "close": round(float(latest.get("close", 0)), 2),
                    "pct_chg": round(float(latest.get("pct_chg", 0)), 2),
                    "change": round(float(latest.get("change", 0)), 2),
                })
        if results:
            ttl = 300 if trading else 3600
            await cache_set(cache_key, results, ttl=ttl)
            return APIResponse(data={"indices": results, "source": "tushare_daily", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))
    except Exception:
        pass

    return APIResponse(data={"indices": [], "source": "none", "update_time": now_ts, "trading": False}, timestamp=int(time.time()),
                       ext_info={"note": "暂无指数数据，请执行数据同步"})


@market_router.get("/mood")
async def market_mood():
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()
    mood_date = date.today().strftime("%Y%m%d")

    # 1. Try AKShare
    try:
        raw = await safe_akshare_call("stock_zh_a_spot_em", cache_key="market_mood")
        if raw is not None:
            mood = _parse_mood_from_akshare(raw)
            if mood.get("up") or mood.get("down"):
                # Get limit up/down from Tushare if available (more accurate)
                try:
                    from app.services.tushare_client import get_limit_list
                    limit_data = await get_limit_list(mood_date)
                    if limit_data:
                        limit_up = sum(1 for d in limit_data if float(d.get("limit", 0) or 0) > 0)
                        limit_down = sum(1 for d in limit_data if float(d.get("limit", 0) or 0) < 0)
                        mood["limit_up"] = limit_up
                        mood["limit_down"] = limit_down
                except Exception:
                    pass
                mood["date"] = mood_date
                mood["source"] = "akshare_realtime"
                mood["update_time"] = now_ts
                return APIResponse(data=mood, timestamp=int(time.time()))
    except Exception:
        pass

    # 2. Fallback: SQLite / Tushare
    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    mood_data = None
    for offset in range(4):
        td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"market:mood:{td}")
        if cached is not None:
            cached["source"] = "tushare_daily"
            cached["update_time"] = now_ts
            return APIResponse(data=cached, timestamp=int(time.time()))

        async with async_session() as sess:
            r = await sess.execute(
                text("SELECT COUNT(*) FILTER (WHERE pct_chg > 0) as up, "
                     "COUNT(*) FILTER (WHERE pct_chg < 0) as down, "
                     "COUNT(*) FILTER (WHERE pct_chg = 0) as flat "
                     "FROM stock_daily WHERE trade_date = :td"),
                {"td": td},
            )
            row = r.first()
            if row and (row[0] or row[1]):
                limit_up = 0
                limit_down = 0
                limit_r = await sess.execute(
                    text("SELECT COUNT(*) FILTER (WHERE pct_chg >= 9.9) as up, "
                         "COUNT(*) FILTER (WHERE pct_chg <= -9.9) as down "
                         "FROM stock_daily WHERE trade_date = :td"),
                    {"td": td},
                )
                lr = limit_r.first()
                if lr:
                    limit_up = lr[0] or 0
                    limit_down = lr[1] or 0

                mood_data = {
                    "up": int(row[0]), "down": int(row[1]), "flat": int(row[2]),
                    "limit_up": limit_up, "limit_down": limit_down,
                    "date": td, "source": "tushare_daily", "update_time": now_ts,
                }
                mood_date = td
                break

    if not mood_data:
        return APIResponse(
            data={"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
                  "date": "", "source": "none", "update_time": now_ts},
            timestamp=int(time.time()),
            ext_info={"note": "暂无市场情绪数据，请执行数据同步"},
        )

    is_today = mood_date == date.today().strftime("%Y%m%d")
    ttl = 300 if is_today else 86400
    await cache_set(f"market:mood:{mood_date}", {k: v for k, v in mood_data.items() if k not in ("source", "update_time")}, ttl=ttl)
    return APIResponse(data=mood_data, timestamp=int(time.time()))


@sector_router.get("/ranking")
async def sector_ranking():
    today = date.today()
    for offset in [1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"sector:ranking:{td}")
        if cached:
            return APIResponse(data={"date": td, "sectors": cached}, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "sectors": []}, timestamp=int(time.time()),
                       ext_info={"note": "请先在管理后台执行数据同步和板块分析计算"})


@review_router.get("/latest")
async def latest_review():
    from app.core.cache import cache_get
    from datetime import date, timedelta
    today = date.today()
    for offset in [0, 1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"review:{td}")
        if cached:
            return APIResponse(data=cached, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "content": {}}, timestamp=int(time.time()),
                       ext_info={"note": "需要先运行数据同步"})


@review_router.get("/daily")
async def review_daily(date: str = ""):
    """按日期查询复盘数据，缓存保留60天"""
    from app.core.cache import cache_get, cache_set
    from datetime import date as _date, timedelta
    from app.services.market_review import MarketReviewEngine

    if not date:
        today = _date.today()
        date = (today - timedelta(days=1)).strftime("%Y%m%d")

    cached = await cache_get(f"review:{date}")
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    engine = MarketReviewEngine()
    review_data = await engine.compute(date)
    if review_data:
        await cache_set(f"review:{date}", review_data, ttl=86400 * 60)

    return APIResponse(data=review_data, timestamp=int(time.time()))


@risk_router.get("")
async def risk_list(page: int = 1, page_size: int = 20):
    """短线风险避雷清单——缓存优先，回退查DB。"""
    from app.core.cache import cache_get
    from app.core.database import async_session
    from app.models.orm.models import RiskListResult
    from sqlalchemy import select, func
    from datetime import date, timedelta

    today = date.today()
    items = []
    for offset in [0, 1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"risk:list:{td}")
        if cached:
            items = cached
            break

    if not items:
        from app.core.cache import cache_set
        async with async_session() as session:
            r = await session.execute(
                select(func.max(RiskListResult.calc_date))
            )
            latest_date = r.scalar_one_or_none()
            if latest_date:
                r = await session.execute(
                    select(RiskListResult).where(RiskListResult.calc_date == latest_date)
                )
                rows = r.scalars().all()
                items = [
                    {"risk_category": row.risk_category, "ts_code": row.ts_code,
                     "stock_name": row.stock_name, "risk_detail": row.risk_detail}
                    for row in rows
                ]
                if items:
                    await cache_set(f"risk:list:{latest_date}", items, ttl=86400)

    if not items:
        return APIResponse(
            data={"total": 0, "page": page, "page_size": page_size, "items": []},
            timestamp=int(time.time()),
            ext_info={"note": "暂无风险扫描数据，请在管理后台执行数据同步后运行风险扫描"},
        )

    total = len(items)
    start = (page - 1) * page_size
    paged = items[start:start + page_size]

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": paged},
        timestamp=int(time.time()),
    )
