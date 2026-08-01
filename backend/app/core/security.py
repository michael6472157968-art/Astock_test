"""JWT 认证与密码哈希。

access_token 15min + refresh_token 7d，bcrypt 哈希密码。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, Request
from jose import JWTError, jwt

from app.core.settings import get_settings

_settings = get_settings()
logger = logging.getLogger("auth")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: int, tier: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=_settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "tier": tier, "type": "access", "exp": expire}
    return jwt.encode(payload, _settings.jwt_secret_key, algorithm=_settings.jwt_algorithm)


def create_refresh_token(user_id: int, tier: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=_settings.refresh_token_expire_days)
    payload = {"sub": str(user_id), "tier": tier, "type": "refresh", "exp": expire}
    return jwt.encode(payload, _settings.jwt_secret_key, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码JWT，抛出 JWTError 若无效/过期。"""
    return jwt.decode(token, _settings.jwt_secret_key, algorithms=[_settings.jwt_algorithm])


# ── FastAPI 认证依赖 ──


def get_current_user(request: Request) -> dict:
    """从 Bearer token 提取 user_id 和 tier。失败抛出 401。"""
    from app.core.exceptions import AuthError
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise AuthError("未提供认证令牌，请先登录")
    try:
        payload = decode_token(auth[7:])
        user_id = int(payload["sub"])
        tier = payload.get("tier", 0)
        return {"user_id": user_id, "tier": tier}
    except (JWTError, KeyError, ValueError):
        raise AuthError("认证令牌无效或已过期，请重新登录")


def require_tier(min_tier: int):
    """工厂函数——返回 FastAPI Depends，确保用户 tier >= min_tier（管理员 tier=99 绕过一切）"""
    from app.core.exceptions import TierDeniedError

    async def _check(user: dict = Depends(get_current_user)) -> dict:
        tier = user["tier"]
        if tier == 99:
            return user
        if tier < min_tier:
            raise TierDeniedError()
        return user

    return _check


# ── 管理员种子 ──


async def seed_admin() -> None:
    """确保管理员账户存在：15381971542 / tier=99。"""
    from app.core.database import async_session
    from app.models.orm.models import User
    from sqlalchemy import select

    admin_phone = "15381971542"
    admin_password = "cbw523718"

    async with async_session() as session:
        result = await session.execute(select(User).where(User.phone == admin_phone))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                phone=admin_phone,
                password_hash=hash_password(admin_password),
                tier=99,
            )
            session.add(user)
            await session.commit()
            logger.info("Admin account created: 15381971542 (tier=99)")
        elif user.tier != 99:
            user.tier = 99
            await session.commit()
            logger.info("Admin account upgraded to tier=99")
        else:
            logger.debug("Admin account already exists")
