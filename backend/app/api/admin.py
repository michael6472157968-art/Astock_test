"""管理 API——手动触发数据同步、查看缓存状态、会员激活码管理。
需要管理员权限 (tier=99)。
"""

from __future__ import annotations

import logging
import random
import string
import time

from fastapi import APIRouter, Depends, HTTPException

from app.core.cache import cache_clear, cache_delete, cache_stats
from app.core.database import async_session
from app.core.scheduler import get_scheduler
from app.core.security import require_tier
from app.core.settings import get_settings
from app.models.orm.models import MembershipCode
from app.models.schemas.common import APIResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/admin", tags=["管理"], dependencies=[Depends(require_tier(99))])
logger = logging.getLogger("admin")
_settings = get_settings()

_CODE_CHARS = string.ascii_uppercase + string.digits
_CODE_BAD = {"0", "O", "1", "I", "5", "S"}  # 易混淆字符排除
_CODE_ALPHABET = [c for c in _CODE_CHARS if c not in _CODE_BAD]


def _gen_code() -> str:
    return "".join(random.choices(_CODE_ALPHABET, k=8))


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
        r2 = await scanner.scan_risk_list()
        return APIResponse(data={"message": "风险扫描已刷新", "risks": str(len(r2))}, timestamp=int(time.time()))
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

        from app.services.stock_pool_engine import StockPoolEngine
        from app.services.sector_analysis import SectorAnalysisEngine
        from app.services.market_review import MarketReviewEngine
        from app.services.risk_scanner import RiskScanner
        await StockPoolEngine().compute_all()
        await SectorAnalysisEngine().compute_all()
        await MarketReviewEngine().compute()
        scanner2 = RiskScanner()
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


# ── 会员激活码管理 ──


class GenCodesRequest(BaseModel):
    code_type: str = Field("monthly", pattern="^(monthly|annual)$")
    count: int = Field(10, ge=1, le=200)


@router.post("/membership/codes")
async def admin_gen_codes(req: GenCodesRequest):
    """生成会员激活码，返回码列表。"""
    codes = []
    async with async_session() as session:
        # 获取当前管理员 ID（虽然 router 级别有 Depends，但这里需要具体 user_id）
        pass
    # 批量生成唯一码
    async with async_session() as session:
        for _ in range(req.count):
            for _ in range(100):  # 最多重试100次防重复
                code = _gen_code()
                existing = await session.execute(select(MembershipCode).where(MembershipCode.code == code))
                if not existing.scalar_one_or_none():
                    break
            session.add(MembershipCode(
                code=code,
                code_type=req.code_type,
                created_by=0,  # system generated
            ))
            codes.append(code)
        await session.commit()

    logger.info(f"Generated {len(codes)} {req.code_type} membership codes")
    return APIResponse(
        data={"codes": codes, "code_type": req.code_type, "count": len(codes)},
        timestamp=int(time.time()),
    )


@router.get("/membership/codes")
async def admin_list_codes():
    """查看所有激活码状态。"""
    async with async_session() as session:
        result = await session.execute(
            select(MembershipCode).order_by(MembershipCode.created_at.desc()).limit(200)
        )
        codes = []
        for c in result.scalars().all():
            codes.append({
                "id": c.id,
                "code": c.code,
                "code_type": c.code_type,
                "is_used": bool(c.is_used),
                "used_by": c.used_by,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "used_at": c.used_at.isoformat() if c.used_at else None,
            })

    return APIResponse(data={"total": len(codes), "items": codes}, timestamp=int(time.time()))
