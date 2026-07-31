"""市场行情 API——板块分析、每日复盘、风险避雷。"""

from __future__ import annotations

import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from app.core.cache import cache_get
from app.models.schemas.common import APIResponse

sector_router = APIRouter(prefix="/api/v1/sector-rotation", tags=["板块轮动"])
review_router = APIRouter(prefix="/api/v1/review", tags=["每日复盘"])
risk_router = APIRouter(prefix="/api/v1/risk-list", tags=["风险避雷"])


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

    # 先查缓存
    cached = await cache_get(f"review:{date}")
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    # 缓存未命中，即时计算并缓存60天
    engine = MarketReviewEngine()
    review_data = await engine.compute(date)
    if review_data:
        await cache_set(f"review:{date}", review_data, ttl=86400 * 60)  # 60天TTL

    return APIResponse(data=review_data, timestamp=int(time.time()))


@risk_router.get("")
async def risk_list(page: int = 1, page_size: int = 20):
    return APIResponse(
        data={"total": 0, "page": page, "page_size": page_size, "items": []},
        timestamp=int(time.time()),
        ext_info={"note": "需要先运行数据同步"},
    )
