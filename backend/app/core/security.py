"""JWT 认证与密码哈希。

access_token 15min + refresh_token 7d，bcrypt 哈希密码。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt

from jose import JWTError, jwt

from app.core.settings import get_settings

_settings = get_settings()


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
