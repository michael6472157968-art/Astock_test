"""用户 API——个人中心、移动端连接。"""

from __future__ import annotations

import logging
import random
import string
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.cache import cache_get, cache_set
from app.core.database import async_session
from app.core.security import create_access_token, create_refresh_token, get_current_user
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import User
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/user", tags=["用户"])
logger = logging.getLogger("user")


def _tier_to_label(tier: int) -> str:
    return {0: "free", 1: "free", 2: "monthly", 3: "annual", 99: "admin"}.get(tier, "free")


def _label_name(tier: int) -> str:
    return {0: "游客", 1: "注册用户", 2: "月度VIP", 3: "年度VIP", 99: "管理员"}.get(tier, "免费用户")


def _mask_phone(phone: str) -> str:
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[7:]}"
    return phone


def _calc_remain(member_expire) -> int | None:
    if member_expire is None:
        return None
    from datetime import datetime
    delta = (member_expire - datetime.now()).days
    return max(0, delta)


@router.get("/profile")
async def user_profile(user: dict = Depends(require_auth)):
    """获取个人中心详细信息。"""
    user_id = user["user_id"]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        return APIResponse(
            data={
                "user_id": u.id,
                "phone": _mask_phone(u.phone),
                "tier": u.tier,
                "member_type": _tier_to_label(u.tier),
                "member_name": _label_name(u.tier),
                "remain_days": _calc_remain(u.member_expire),
                "member_expire": u.member_expire.isoformat() if u.member_expire else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "is_admin": u.tier == 99,
            },
            timestamp=int(time.time()),
        )


@router.get("/connection")
async def user_connection(request: Request, user: dict = Depends(require_auth)):
    """生成移动端连接 token（6位数字，5分钟有效）。"""
    token = "".join(random.choices(string.digits, k=6))
    await cache_set(f"connection:token:{token}", user["user_id"], ttl=300)

    host = request.headers.get("host", "localhost:8000")
    scheme = request.headers.get("x-forwarded-proto", "http")
    url = f"{scheme}://{host}/mobile-login.html?token={token}"

    return APIResponse(
        data={"token": token, "url": url, "expires_in": 300},
        timestamp=int(time.time()),
    )


class MobileLoginRequest(BaseModel):
    token: str = Field(..., min_length=6, max_length=6)


@router.post("/mobile-login")
async def mobile_login(req: MobileLoginRequest):
    """移动端通过 token 登录。"""
    from app.core.security import create_access_token, create_refresh_token

    user_id = await cache_get(f"connection:token:{req.token}")
    if not user_id:
        raise HTTPException(status_code=401, detail="连接码无效或已过期")

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        access = create_access_token(u.id, u.tier)
        refresh = create_refresh_token(u.id, u.tier)

        # 用完即删
        from app.core.cache import cache_delete
        await cache_delete(f"connection:token:{req.token}")

        return APIResponse(
            data={
                "user_id": u.id,
                "phone": _mask_phone(u.phone),
                "tier": u.tier,
                "member_expire": u.member_expire.isoformat() if u.member_expire else None,
                "access_token": access,
                "refresh_token": refresh,
            },
            timestamp=int(time.time()),
        )
