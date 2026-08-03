"""用户认证 API——手机号注册/登录，JWT Token 颁发。"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import async_session
from app.core.security import (create_access_token, create_refresh_token,
                                decode_token, hash_password, verify_password)
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth, require_auth_optional
from app.models.orm.models import CreditLedger, User
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])
logger = logging.getLogger("auth")
_settings = get_settings()


class RegisterRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$", description="手机号")
    password: str = Field(..., min_length=6, max_length=32)
    verify_code: str = Field(default="000000")


class LoginRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    password: str = Field(..., min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/register")
async def register(req: RegisterRequest):
    """手机号注册。验证码测试阶段固定为000000。"""
    async with async_session() as session:
        existing = await session.execute(select(User).where(User.phone == req.phone))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="该手机号已注册")

        user = User(
            phone=req.phone,
            password_hash=hash_password(req.password),
            tier=1,  # 注册用户默认 tier=1
            credits=10,  # 注册赠送10积分
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # 注册积分流水
        session.add(CreditLedger(
            user_id=user.id,
            amount=10,
            type="register",
            ref_id="",
            balance_after=10,
            note="注册赠送",
        ))
        await session.commit()

        access = create_access_token(user.id, user.tier)
        refresh = create_refresh_token(user.id, user.tier)

        from app.middleware.access_log import log_access

        log_access(user.id, "/api/v1/auth/register", "", "")

        return APIResponse(
            data={
                "user_id": user.id,
                "phone": _mask_phone(user.phone),
                "tier": user.tier,
                "access_token": access,
                "refresh_token": refresh,
            },
            timestamp=int(time.time()),
        )


@router.post("/login")
async def login(req: LoginRequest):
    """手机号+密码登录。"""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.phone == req.phone))
        user = result.scalar_one_or_none()
        if not user or not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="手机号或密码错误")

        access = create_access_token(user.id, user.tier)
        refresh = create_refresh_token(user.id, user.tier)

        from datetime import datetime
        remain = None
        if user.member_expire:
            remain = max(0, (user.member_expire - datetime.now()).days)

        from app.middleware.access_log import log_access

        log_access(user.id, "/api/v1/auth/login", "", "")

        return APIResponse(
            data={
                "user_id": user.id,
                "phone": _mask_phone(user.phone),
                "tier": user.tier,
                "member_type": _tier_to_label(user.tier),
                "member_name": _label_name(user.tier),
                "remain_days": remain,
                "member_expire": user.member_expire.isoformat() if user.member_expire else None,
                "credits": user.credits or 0,
                "access_token": access,
                "refresh_token": refresh,
            },
            timestamp=int(time.time()),
        )


@router.post("/refresh")
async def refresh_token(req: RefreshRequest):
    """刷新 access_token。"""
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="无效的refresh token")
        user_id = int(payload["sub"])
        tier = payload.get("tier", 0)
    except Exception:
        raise HTTPException(status_code=401, detail="Token无效或已过期")

    access = create_access_token(user_id, tier)
    return APIResponse(
        data={"access_token": access},
        timestamp=int(time.time()),
    )


@router.get("/profile")
async def profile(user: dict = Depends(require_auth)):
    """获取当前用户信息（需登录）。"""
    user_id, tier = user["user_id"], user["tier"]
    if user_id is None:
        raise HTTPException(status_code=401, detail="请先登录")

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        from datetime import datetime
        remain = None
        if user.member_expire:
            remain = max(0, (user.member_expire - datetime.now()).days)

        return APIResponse(
            data={
                "user_id": user.id,
                "phone": _mask_phone(user.phone),
                "tier": user.tier,
                "member_type": _tier_to_label(user.tier),
                "member_name": _label_name(user.tier),
                "remain_days": remain,
                "member_expire": user.member_expire.isoformat() if user.member_expire else None,
                "credits": user.credits or 0,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            },
            timestamp=int(time.time()),
        )


def _mask_phone(phone: str) -> str:
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[7:]}"
    return phone


def _tier_to_label(tier: int) -> str:
    return {0: "free", 1: "free", 2: "monthly", 3: "annual", 99: "admin"}.get(tier, "free")


def _label_name(tier: int) -> str:
    return {0: "游客", 1: "注册用户", 2: "月度VIP", 3: "年度VIP", 99: "管理员"}.get(tier, "免费用户")
