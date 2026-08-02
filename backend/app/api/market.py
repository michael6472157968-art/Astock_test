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

market_router = APIRouter(prefix="/api/v1/market", tags=["市场概览"])

INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]


def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 540 <= t <= 930


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


@market_router.get("/index")
async def market_index():
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    today_key = date.today().strftime("%Y%m%d")
    cache_key = f"market:index:{today_key}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return APIResponse(data={"indices": cached, "source": "tushare", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))

    results = []
    async with async_session() as sess:
        for code, name in INDEX_CODES:
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
        return APIResponse(data={"indices": results, "source": "tushare", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))

    # DB 为空时调 Tushare API
    try:
        from app.services.tushare_client import call_tushare
        results = []
        for code, name in INDEX_CODES:
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
            return APIResponse(data={"indices": results, "source": "tushare", "update_time": now_ts, "trading": trading}, timestamp=int(time.time()))
    except Exception:
        pass

    return APIResponse(data={"indices": [], "source": "none", "update_time": now_ts, "trading": False}, timestamp=int(time.time()),
                       ext_info={"note": "暂无指数数据，请执行数据同步"})


@market_router.get("/mood")
async def market_mood():
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    today = date.today()
    mood_data = None

    for offset in range(4):
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"market:mood:{td}")
        if cached is not None:
            cached["source"] = "tushare"
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
                    "date": td, "source": "tushare", "update_time": now_ts,
                }
                break

    if not mood_data:
        return APIResponse(
            data={"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
                  "date": "", "source": "none", "update_time": now_ts},
            timestamp=int(time.time()),
            ext_info={"note": "暂无市场情绪数据，请执行数据同步"},
        )

    is_today = mood_data["date"] == today.strftime("%Y%m%d")
    ttl = 300 if is_today else 86400
    cache_data = {k: v for k, v in mood_data.items() if k not in ("source", "update_time")}
    await cache_set(f"market:mood:{mood_data['date']}", cache_data, ttl=ttl)
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
