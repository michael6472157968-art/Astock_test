"""调试管理 API——仅 DEBUG 模式下启用。手动触发数据同步、查看缓存状态。"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from app.core.cache import cache_clear, cache_delete, cache_stats
from app.core.scheduler import get_scheduler
from app.core.settings import get_settings
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/admin", tags=["管理"])
logger = logging.getLogger("admin")
_settings = get_settings()


@router.get("/cache/stats")
async def admin_cache_stats():
    stats = await cache_stats()
    return APIResponse(data=stats, timestamp=int(time.time()))


@router.delete("/cache/{key}")
async def admin_cache_delete(key: str):
    await cache_delete(key)
    return APIResponse(data={"deleted": key}, timestamp=int(time.time()))


@router.post("/cache/clear-all")
async def admin_cache_clear():
    await cache_clear()
    return APIResponse(data={"message": "缓存已清空"}, timestamp=int(time.time()))


@router.post("/cache/refresh/pool")
async def admin_refresh_pool():
    try:
        from app.services.stock_pool_engine import StockPoolEngine
        from app.services.sector_analysis import SectorAnalysisEngine
        from app.services.market_review import MarketReviewEngine
        from app.services.risk_scanner import RiskScanner
        result = await StockPoolEngine().compute_all()
        await SectorAnalysisEngine().compute_all()
        await MarketReviewEngine().compute()
        scanner = RiskScanner()
        await scanner.scan_all()
        await scanner.scan_risk_list()
        return APIResponse(data={"message": "选股池+板块+复盘+风险已全部刷新"}, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cache/refresh/review")
async def admin_refresh_review():
    try:
        from app.services.market_review import MarketReviewEngine
        result = await MarketReviewEngine().compute()
        return APIResponse(data={"message": "复盘已刷新", "result": str(result)}, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cache/refresh/risk")
async def admin_refresh_risk():
    try:
        from app.services.risk_scanner import RiskScanner
        scanner = RiskScanner()
        r1 = await scanner.scan_all()
        r2 = await scanner.scan_risk_list()
        return APIResponse(data={"message": "风险扫描已刷新", "alerts": str(r1), "risks": str(len(r2))}, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cache/refresh/sector")
async def admin_refresh_sector():
    try:
        from app.services.sector_analysis import SectorAnalysisEngine
        result = await SectorAnalysisEngine().compute_all()
        return APIResponse(data={"message": "板块分析已刷新", "result": str(result)}, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tasks")
async def admin_list_tasks():
    sched = get_scheduler()
    if sched is None:
        return APIResponse(data={"tasks": [], "message": "Scheduler未启动"}, timestamp=int(time.time()))
    tasks = []
    for job in sched.get_jobs():
        tasks.append({"id": job.id, "name": job.name, "next_run": str(job.next_run_time)})
    return APIResponse(data={"tasks": tasks}, timestamp=int(time.time()))


@router.post("/tasks/run-daily-batch")
async def admin_run_daily_batch():
    try:
        from app.services.data_sync import sync_daily_data, sync_stock_basic
        stock_count = await sync_stock_basic()
        daily_count = await sync_daily_data()

        # 数据同步后自动更新所有板块缓存
        from app.services.stock_pool_engine import StockPoolEngine
        from app.services.sector_analysis import SectorAnalysisEngine
        from app.services.market_review import MarketReviewEngine
        from app.services.risk_scanner import RiskScanner
        await StockPoolEngine().compute_all()
        await SectorAnalysisEngine().compute_all()
        await MarketReviewEngine().compute()
        scanner2 = RiskScanner()
        await scanner2.scan_all()
        await scanner2.scan_risk_list()

        return APIResponse(
            data={"stock_synced": stock_count, "daily_synced": daily_count},
            timestamp=int(time.time()),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/tushare")
async def admin_tushare_stats():
    from app.core.cache import cache_get
    from datetime import date
    today = date.today().isoformat()
    daily = await cache_get(f"tushare_daily:{today}") or 0
    return APIResponse(
        data={"daily_calls": daily, "daily_limit": _settings.tushare_daily_credit_limit},
        timestamp=int(time.time()),
    )
