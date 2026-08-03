"""积分 API——签到、流水、余额查询。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc

from app.core.database import async_session
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import CheckinRecord, CreditLedger, User
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/credits", tags=["积分"])
SIGNIN_REWARD_FREE = 3
SIGNIN_REWARD_VIP = 5
SIGNIN_STREAK_BONUS = 5  # 连续7天额外奖励


_CREDIT_TYPE_LABEL = {
    "register": "注册赠送",
    "checkin": "每日签到",
    "activation": "会员激活",
    "guess": "大盘竞猜",
    "diagnosis": "诊股消耗",
    "ai_analysis": "AI分析",
    "admin": "管理员调整",
}


@router.get("/balance")
async def get_balance(user: dict = Depends(require_auth)):
    """当前用户积分余额。"""
    user_id = user["user_id"]
    async with async_session() as session:
        result = await session.execute(select(User.credits).where(User.id == user_id))
        credits = result.scalar()
        if credits is None:
            raise HTTPException(status_code=404, detail="用户不存在")
    return APIResponse(data={"credits": credits}, timestamp=int(time.time()))


@router.get("/ledger")
async def get_ledger(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_auth),
):
    """积分流水（分页），按时间倒序。"""
    user_id = user["user_id"]
    async with async_session() as session:
        total_q = select(func.count()).select_from(CreditLedger).where(
            CreditLedger.user_id == user_id
        )
        total = (await session.execute(total_q)).scalar() or 0

        items_q = (
            select(CreditLedger)
            .where(CreditLedger.user_id == user_id)
            .order_by(desc(CreditLedger.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.execute(items_q)).scalars().all()

        items = []
        for r in rows:
            items.append({
                "id": r.id,
                "amount": r.amount,
                "type": r.type,
                "type_label": _CREDIT_TYPE_LABEL.get(r.type, r.type),
                "ref_id": r.ref_id,
                "balance_after": r.balance_after,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        timestamp=int(time.time()),
    )


@router.post("/checkin")
async def do_checkin(user: dict = Depends(require_auth)):
    """每日签到——免费+3/VIP+5，连续7天额外+5。"""
    user_id, tier = user["user_id"], user["tier"]
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    async with async_session() as session:
        # 防重复
        existing = await session.execute(
            select(CheckinRecord).where(
                CheckinRecord.user_id == user_id, CheckinRecord.date == today
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="今日已签到")

        # 连续天数
        prev = await session.execute(
            select(CheckinRecord)
            .where(CheckinRecord.user_id == user_id, CheckinRecord.date == yesterday)
        )
        prev_record = prev.scalar_one_or_none()
        streak = (prev_record.streak if prev_record else 0) + 1

        # 计算积分
        base = SIGNIN_REWARD_VIP if tier >= 2 else SIGNIN_REWARD_FREE
        bonus = SIGNIN_STREAK_BONUS if streak == 7 else 0
        total_credits = base + bonus

        # 获取用户
        u_result = await session.execute(select(User).where(User.id == user_id))
        u = u_result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 更新积分
        u.credits = u.credits + total_credits

        # 签到记录
        record = CheckinRecord(
            user_id=user_id, date=today, streak=streak, credits=total_credits
        )
        session.add(record)

        # 积分流水
        note = f"签到 +{base}"
        if bonus:
            note += f" (连续{streak}天额外+{bonus})"
        session.add(CreditLedger(
            user_id=user_id,
            amount=total_credits,
            type="checkin",
            ref_id=today,
            balance_after=u.credits,
            note=note,
        ))

        await session.commit()

    return APIResponse(
        data={
            "credits": total_credits,
            "balance": u.credits,
            "streak": streak,
            "base": base,
            "bonus": bonus,
        },
        timestamp=int(time.time()),
    )


@router.get("/checkin/status")
async def checkin_status(user: dict = Depends(require_auth)):
    """签到状态：今天是否已签到、连续天数。"""
    user_id = user["user_id"]
    today = date.today().isoformat()

    async with async_session() as session:
        today_record = await session.execute(
            select(CheckinRecord).where(
                CheckinRecord.user_id == user_id, CheckinRecord.date == today
            )
        )
        today_r = today_record.scalar_one_or_none()

        # 最近30天签到日期（用于日历展示）
        month_ago = (date.today() - timedelta(days=29)).isoformat()
        month_records = await session.execute(
            select(CheckinRecord.date)
            .where(CheckinRecord.user_id == user_id, CheckinRecord.date >= month_ago)
            .order_by(CheckinRecord.date)
        )
        dates = [r[0] for r in month_records.all()]

    return APIResponse(
        data={
            "checked_in_today": today_r is not None,
            "streak": today_r.streak if today_r else 0,
            "today_credits": today_r.credits if today_r else 0,
            "checkin_dates": dates,
        },
        timestamp=int(time.time()),
    )
