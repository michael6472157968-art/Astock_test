"""会员 API——激活码充值、会员状态查询。"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import async_session
from app.core.security import create_access_token, create_refresh_token
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import CreditLedger, MembershipCode, User
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/membership", tags=["会员"])
logger = logging.getLogger("membership")
_settings = get_settings()


def _tier_to_label(tier: int) -> str:
    return {0: "free", 1: "free", 2: "monthly", 3: "annual", 99: "admin"}.get(tier, "free")


def _label_name(tier: int) -> str:
    return {0: "游客", 1: "注册用户", 2: "月度VIP", 3: "年度VIP", 99: "管理员"}.get(tier, "免费用户")


def _calc_remain(member_expire) -> int | None:
    if member_expire is None:
        return None
    delta = (member_expire - datetime.now()).days
    return max(0, delta)


@router.get("/status")
async def membership_status(user: dict = Depends(require_auth)):
    """查询当前用户会员状态。"""
    user_id, tier = user["user_id"], user["tier"]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        remain = _calc_remain(u.member_expire)
        return APIResponse(
            data={
                "tier": u.tier,
                "member_type": _tier_to_label(u.tier),
                "member_name": _label_name(u.tier),
                "remain_days": remain,
                "member_expire": u.member_expire.isoformat() if u.member_expire else None,
                "is_vip": u.tier >= 2 or u.tier == 99,
            },
            timestamp=int(time.time()),
        )


class ActivateRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=20)


@router.post("/activate")
async def membership_activate(req: ActivateRequest, user: dict = Depends(require_auth)):
    """使用激活码充值会员。"""
    user_id, _ = user["user_id"], user["tier"]
    code = req.code.strip().upper()

    async with async_session() as session:
        result = await session.execute(
            select(MembershipCode).where(MembershipCode.code == code)
        )
        mcode = result.scalar_one_or_none()
        if not mcode:
            raise HTTPException(status_code=400, detail="激活码无效")
        if mcode.is_used:
            raise HTTPException(status_code=400, detail="该激活码已被使用")

        # 获取用户当前数据
        uresult = await session.execute(select(User).where(User.id == user_id))
        u = uresult.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 计算新到期时间（支持叠加）
        now = datetime.now()
        if mcode.code_type == "monthly":
            days = 30
            new_tier = 2
        else:
            days = 365
            new_tier = 3

        # 如果当前有有效会员，从当前到期日开始叠加；否则从今天开始
        if u.member_expire and u.member_expire > now:
            base = u.member_expire
        else:
            base = now
        new_expire = base + timedelta(days=days)

        # 更新用户
        if u.tier < new_tier or (u.tier == new_tier and u.member_expire and u.member_expire > now):
            # 同类型叠加：只延长时间
            u.tier = max(u.tier, new_tier)
        else:
            u.tier = new_tier
        u.member_expire = new_expire

        # 标记激活码已用
        mcode.is_used = 1
        mcode.used_by = user_id
        mcode.used_at = now

        # 激活赠送积分
        credit_bonus = 100 if mcode.code_type == "monthly" else 500
        u.credits = (u.credits or 0) + credit_bonus
        session.add(CreditLedger(
            user_id=user_id,
            amount=credit_bonus,
            type="activation",
            ref_id=code,
            balance_after=u.credits,
            note=f"{'月度' if mcode.code_type == 'monthly' else '年度'}会员激活赠送",
        ))

        await session.commit()

        # 签发含新 tier 的 token
        access = create_access_token(u.id, u.tier)
        refresh = create_refresh_token(u.id, u.tier)

        remain = _calc_remain(new_expire)
        logger.info(f"User {user_id} activated {mcode.code_type} membership, tier={u.tier}, expire={new_expire}")

        return APIResponse(
            data={
                "tier": u.tier,
                "member_type": _tier_to_label(u.tier),
                "member_name": _label_name(u.tier),
                "remain_days": remain,
                "member_expire": new_expire.isoformat(),
                "credits": u.credits,
                "credit_bonus": credit_bonus,
                "access_token": access,
                "refresh_token": refresh,
            },
            timestamp=int(time.time()),
        )
