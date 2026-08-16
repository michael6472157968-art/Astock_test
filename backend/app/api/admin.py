"""管理 API——手动触发数据同步、查看缓存状态、会员激活码管理、用户管理。
需要管理员权限 (tier=99)。
"""

from __future__ import annotations

import logging
import random
import string
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.cache import cache_clear, cache_delete, cache_stats
from app.core.database import async_session
from app.core.scheduler import get_scheduler
from app.core.security import require_tier, hash_password, get_current_user
from app.core.settings import get_settings
from app.models.orm.models import (
    CheckinRecord, CreditLedger, MarketGuess,
    MembershipCode, User,
)
from app.models.schemas.common import APIResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, text

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
        return APIResponse(data={"message": "选股池+短线优选+板块+复盘+风险已全部刷新"}, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cache/refresh/review")
async def admin_refresh_review():
    try:
        from datetime import datetime, timezone
        from app.services.market_review import MarketReviewEngine
        from app.api.market import (
            _ensure_review_table, _set_latest_flag, _save_review_meta,
            _purge_expired_reviews, cache_delete,
        )
        await _ensure_review_table()
        await _purge_expired_reviews()
        result = await MarketReviewEngine().compute()
        trade_date = result.get("date", "")
        if trade_date and result.get("content", {}).get("total"):
            from app.utils.trading_calendar import get_latest_trade_date
            latest = await get_latest_trade_date()
            generated_at = datetime.now(timezone.utc).isoformat()
            is_latest_flag = 1 if trade_date == latest else 0
            if is_latest_flag:
                await _set_latest_flag(trade_date)
            await _save_review_meta(trade_date, generated_at, is_latest_flag)
            await cache_delete(f"review:{trade_date}")
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
        from app.services.data_sync import (sync_cyq_perf, sync_daily_basic, sync_daily_data, sync_dc_index,
                                             sync_limit_list, sync_margin, sync_moneyflow_hsgt, sync_stock_basic,
                                             sync_top_inst, sync_top_list)
        stock_count = await sync_stock_basic()
        daily_count = await sync_daily_data()
        daily_basic_count = await sync_daily_basic()
        limit_count = await sync_limit_list()
        margin_count = await sync_margin()
        moneyflow_count = await sync_moneyflow_hsgt()
        cyq_count = await sync_cyq_perf()
        top_list_count = await sync_top_list()
        top_inst_count = await sync_top_inst()
        dc_index_count = await sync_dc_index()

        from app.services.alert_engine import AlertEngine
        await AlertEngine().scan_all(str(date.today()))

        from app.services.stock_pool_engine import StockPoolEngine
        from app.services.sector_analysis import SectorAnalysisEngine
        from app.services.market_review import MarketReviewEngine
        from app.services.risk_scanner import RiskScanner
        from app.api.market import _ensure_review_table, _set_latest_flag, _save_review_meta, _purge_expired_reviews, cache_delete as _cache_delete
        from datetime import datetime as _datetime, timezone as _timezone
        await _ensure_review_table()
        await _purge_expired_reviews()
        await StockPoolEngine().compute_all()
        await SectorAnalysisEngine().compute_all()
        review_result = await MarketReviewEngine().compute()
        trade_date = review_result.get("date", "")
        if trade_date and review_result.get("content", {}).get("total"):
            from app.utils.trading_calendar import get_latest_trade_date as _gltd
            latest = await _gltd()
            gen_at = _datetime.now(_timezone.utc).isoformat()
            is_latest_flag = 1 if trade_date == latest else 0
            if is_latest_flag:
                await _set_latest_flag(trade_date)
            await _save_review_meta(trade_date, gen_at, is_latest_flag)
            await _cache_delete(f"review:{trade_date}")
        scanner2 = RiskScanner()
        await scanner2.scan_risk_list()

        warnings = []
        if daily_count == 0:
            warnings.append("日线数据同步为0——Tushare可能尚未更新今日行情，已接管昨日选股池/板块数据")
        if stock_count == 0:
            warnings.append("股票基础信息同步为0")
        if limit_count <= 0:
            warnings.append("涨跌停同步失败(limit_list需社区贡献解锁或积分升级)，已降级为pct_chg估算")

        return APIResponse(
            data={"stock_synced": stock_count, "daily_synced": daily_count,
                  "daily_basic_synced": daily_basic_count,
                  "limit_synced": limit_count, "margin_synced": margin_count,
                  "moneyflow_synced": moneyflow_count,
                  "warnings": warnings},
            timestamp=int(time.time()),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tasks/sync-historical")
async def admin_sync_historical():
    """手动触发 120 天历史日线数据同步（首次安装后可用）。"""
    try:
        from app.services.data_sync import sync_historical_daily
        result = await sync_historical_daily(days=120)
        return APIResponse(data=result, timestamp=int(time.time()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tushare/stats")
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
async def admin_gen_codes(req: GenCodesRequest, user: dict = Depends(get_current_user)):
    """生成会员激活码，返回码列表。"""
    admin_id = user["user_id"]
    codes = []
    async with async_session() as session:
        for _ in range(req.count):
            for _ in range(100):
                code = _gen_code()
                existing = await session.execute(select(MembershipCode).where(MembershipCode.code == code))
                if not existing.scalar_one_or_none():
                    break
            session.add(MembershipCode(
                code=code,
                code_type=req.code_type,
                created_by=admin_id,
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


# ── 用户管理 ──


class AdjustCreditsRequest(BaseModel):
    amount: int  # 正数增加，负数减少
    note: str = Field(..., min_length=1, max_length=200)


class AdjustTierRequest(BaseModel):
    tier: int = Field(..., ge=0, le=99)
    member_expire: str | None = None  # ISO date or null


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=32)


@router.get("/users/stats")
async def admin_user_stats():
    """统计概览：总用户/今日新增/本周新增/等级分布/今日诊股次数。"""
    today_str = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
        today_new = (await session.execute(
            select(func.count()).select_from(User).where(func.date(User.created_at) == today_str)
        )).scalar() or 0
        week_new = (await session.execute(
            select(func.count()).select_from(User).where(func.date(User.created_at) >= week_ago)
        )).scalar() or 0
        tier_dist = {}
        for tier_val in [0, 1, 2, 3, 99]:
            cnt = (await session.execute(
                select(func.count()).select_from(User).where(User.tier == tier_val)
            )).scalar() or 0
            if cnt > 0:
                tier_dist[str(tier_val)] = cnt
        today_diag = (await session.execute(
            select(func.count()).select_from(CreditLedger).where(
                CreditLedger.type == "diagnosis",
                func.date(CreditLedger.created_at) == today_str,
            )
        )).scalar() or 0

    return APIResponse(
        data={
            "total_users": total,
            "today_new": today_new,
            "week_new": week_new,
            "tier_distribution": tier_dist,
            "today_diagnosis_count": today_diag,
        },
        timestamp=int(time.time()),
    )


@router.get("/users")
async def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=50),
    tier_filter: int | None = Query(None),
):
    """用户列表——支持手机号搜索、等级筛选、分页。"""
    async with async_session() as session:
        base_q = select(User)
        count_q = select(func.count()).select_from(User)

        if search:
            base_q = base_q.where(User.phone.contains(search))
            count_q = count_q.where(User.phone.contains(search))
        if tier_filter is not None:
            base_q = base_q.where(User.tier == tier_filter)
            count_q = count_q.where(User.tier == tier_filter)

        total = (await session.execute(count_q)).scalar() or 0
        rows = (await session.execute(
            base_q.order_by(desc(User.id)).offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()

        items = []
        for u in rows:
            phone = u.phone
            masked = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone
            items.append({
                "id": u.id,
                "phone_masked": masked,
                "tier": u.tier,
                "credits": u.credits or 0,
                "is_active": bool(u.is_active),
                "member_expire": u.member_expire.isoformat() if u.member_expire else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        timestamp=int(time.time()),
    )


@router.get("/users/{user_id}")
async def admin_user_detail(user_id: int):
    """用户详情——基本信息+积分流水+签到记录+诊股统计+会员历史。"""
    async with async_session() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        phone = u.phone
        masked = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone

        user_info = {
            "id": u.id,
            "phone_masked": masked,
            "tier": u.tier,
            "credits": u.credits or 0,
            "is_active": bool(u.is_active),
            "member_expire": u.member_expire.isoformat() if u.member_expire else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "updated_at": u.updated_at.isoformat() if u.updated_at else None,
        }

    # 积分流水（最近50条）
    async with async_session() as session:
        ledger_rows = (await session.execute(
            select(CreditLedger).where(CreditLedger.user_id == user_id)
            .order_by(desc(CreditLedger.id)).limit(50)
        )).scalars().all()
        ledger = [{
            "id": r.id,
            "amount": r.amount,
            "type": r.type,
            "ref_id": r.ref_id,
            "balance_after": r.balance_after,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in ledger_rows]

    # 签到日历（最近30天）
    async with async_session() as session:
        month_ago = (date.today() - timedelta(days=29)).isoformat()
        checkin_rows = (await session.execute(
            select(CheckinRecord.date, CheckinRecord.credits)
            .where(CheckinRecord.user_id == user_id, CheckinRecord.date >= month_ago)
            .order_by(CheckinRecord.date)
        )).all()
        checkin_dates = [{"date": r[0], "credits": r[1]} for r in checkin_rows]

    # 诊股统计
    async with async_session() as session:
        diag_count = (await session.execute(
            select(func.count()).select_from(CreditLedger).where(
                CreditLedger.user_id == user_id, CreditLedger.type == "diagnosis"
            )
        )).scalar() or 0
        ai_count = (await session.execute(
            select(func.count()).select_from(CreditLedger).where(
                CreditLedger.user_id == user_id, CreditLedger.type == "ai_analysis"
            )
        )).scalar() or 0

    # 竞猜统计
    async with async_session() as session:
        guess_total = (await session.execute(
            select(func.count()).select_from(MarketGuess).where(MarketGuess.user_id == user_id)
        )).scalar() or 0
        guess_correct = (await session.execute(
            select(func.count()).select_from(MarketGuess).where(
                MarketGuess.user_id == user_id, MarketGuess.score_change >= 5
            )
        )).scalar() or 0

    # 会员历史（从流水查激活记录）
    async with async_session() as session:
        member_rows = (await session.execute(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id, CreditLedger.type == "activation"
            ).order_by(desc(CreditLedger.id)).limit(10)
        )).scalars().all()
        member_history = [{
            "amount": r.amount,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in member_rows]

    return APIResponse(
        data={
            "user": user_info,
            "ledger": ledger,
            "checkin_dates": checkin_dates,
            "diagnosis_stats": {"total": diag_count, "ai_analysis": ai_count},
            "guess_stats": {"total": guess_total, "correct": guess_correct},
            "member_history": member_history,
        },
        timestamp=int(time.time()),
    )


@router.post("/users/{user_id}/credits")
async def admin_adjust_credits(user_id: int, req: AdjustCreditsRequest):
    """调整积分——正数增加，负数减少，必填备注。"""
    async with async_session() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        u.credits = (u.credits or 0) + req.amount
        session.add(CreditLedger(
            user_id=user_id,
            amount=req.amount,
            type="admin",
            ref_id=str(user_id),
            balance_after=u.credits,
            note=req.note,
        ))
        await session.commit()

        return APIResponse(
            data={"user_id": user_id, "credits": u.credits, "change": req.amount},
            timestamp=int(time.time()),
        )


@router.post("/users/{user_id}/tier")
async def admin_adjust_tier(user_id: int, req: AdjustTierRequest):
    """调整等级——含可选到期日。"""
    async with async_session() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        u.tier = req.tier
        if req.member_expire:
            try:
                u.member_expire = datetime.fromisoformat(req.member_expire)
            except ValueError:
                raise HTTPException(status_code=400, detail="member_expire 格式无效，应为 ISO 日期")
        else:
            u.member_expire = None

        await session.commit()

        return APIResponse(
            data={
                "user_id": user_id,
                "tier": u.tier,
                "member_expire": u.member_expire.isoformat() if u.member_expire else None,
            },
            timestamp=int(time.time()),
        )


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, req: ResetPasswordRequest):
    """重置密码。"""
    async with async_session() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        u.password_hash = hash_password(req.new_password)
        await session.commit()

        return APIResponse(
            data={"user_id": user_id, "message": "密码已重置"},
            timestamp=int(time.time()),
        )


@router.post("/users/{user_id}/toggle-active")
async def admin_toggle_active(user_id: int):
    """禁用/启用账号。"""
    async with async_session() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        u.is_active = 1 if u.is_active == 0 else 0
        await session.commit()

        return APIResponse(
            data={"user_id": user_id, "is_active": bool(u.is_active)},
            timestamp=int(time.time()),
        )


# ── 仪表盘趋势 ──

@router.get("/dashboard/trend")
async def admin_dashboard_trend():
    """近7天每日新增用户 + 诊股次数 + API调用量趋势。"""
    today_str = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=6)).isoformat()

    async with async_session() as session:
        # 每日新增用户
        new_users_raw = (await session.execute(
            text("SELECT DATE(created_at) as d, COUNT(*) FROM users WHERE DATE(created_at) >= :wa GROUP BY d ORDER BY d"),
            {"wa": week_ago}
        )).all()
        # 每日诊股
        diag_raw = (await session.execute(
            text("SELECT DATE(created_at) as d, COUNT(*) FROM credit_ledger WHERE type = 'diagnosis' AND DATE(created_at) >= :wa GROUP BY d ORDER BY d"),
            {"wa": week_ago}
        )).all()
        # 每日API调用(access_log)
        try:
            api_raw = (await session.execute(
                text("SELECT DATE(access_time) as d, COUNT(*) FROM access_logs WHERE DATE(access_time) >= :wa GROUP BY d ORDER BY d"),
                {"wa": week_ago}
            )).all()
        except Exception:
            api_raw = []

        # 填充7天
        days = []
        for i in range(7):
            d = (date.today() - timedelta(days=6 - i)).isoformat()
            nu = next((r[1] for r in new_users_raw if r[0] == d), 0)
            dg = next((r[1] for r in diag_raw if r[0] == d), 0)
            ap = next((r[1] for r in api_raw if r[0] == d), 0)
            days.append({"date": d, "new_users": nu, "diagnosis_count": dg, "api_calls": ap})

    return APIResponse(data={"days": days}, timestamp=int(time.time()))


# ── 系统日志 ──

@router.get("/logs")
async def admin_logs(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     user_id: int | None = Query(None)):
    """最近200条访问日志，支持按用户筛选。"""
    async with async_session() as session:
        base_q = "SELECT id, user_id, phone, path, access_time FROM access_logs"
        count_q = "SELECT COUNT(*) FROM access_logs"
        params = {}
        if user_id:
            base_q += " WHERE user_id = :uid"
            count_q += " WHERE user_id = :uid"
            params["uid"] = user_id

        total = (await session.execute(text(count_q), params)).scalar() or 0
        rows = (await session.execute(
            text(base_q + " ORDER BY access_time DESC LIMIT :lim OFFSET :off"),
            {**params, "lim": page_size, "off": (page - 1) * page_size}
        )).all()

        items = [{
            "id": r[0],
            "user_id": r[1],
            "phone": (r[2][:3] + "****" + r[2][-4:]) if r[2] and len(r[2]) >= 7 else (r[2] or ""),
            "path": r[3],
            "access_time": r[4],
        } for r in rows]

    return APIResponse(data={"total": total, "page": page, "page_size": page_size, "items": items}, timestamp=int(time.time()))


# ── 站点配置 ──

class SiteConfigRequest(BaseModel):
    site_url: str = Field("", max_length=256)

_SITE_CONFIG_KEY = "admin:site_config"


@router.get("/site-config")
async def admin_get_site_config():
    """获取站点配置（管理员可读）。"""
    from app.core.cache import cache_get
    config = await cache_get(_SITE_CONFIG_KEY) or {"site_url": ""}
    return APIResponse(data=config, timestamp=int(time.time()))


@router.put("/site-config")
async def admin_set_site_config(req: SiteConfigRequest):
    """设置站点配置（管理员可写）。"""
    from app.core.cache import cache_set
    config = {"site_url": req.site_url.strip().rstrip("/")}
    await cache_set(_SITE_CONFIG_KEY, config, ttl=None)
    return APIResponse(data=config, timestamp=int(time.time()))

