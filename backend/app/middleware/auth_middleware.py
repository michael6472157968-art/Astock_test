"""auth middleware — JWT 验证 + 访问日志记录。

支持开发环境下的 .dev_token 永久 token 绕过 —— 仅在 debug=True 或 ENV=dev 时生效。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from app.core.security import decode_token
from app.middleware.access_log import log_access

logger = logging.getLogger("auth_middleware")


def _is_dev_env() -> bool:
    """开发环境检测：debug 模式 或 ENV=dev。"""
    try:
        from app.core.settings import get_settings
        if get_settings().debug:
            return True
    except Exception:
        pass
    return os.getenv("ENV", "").lower() == "dev"


def _load_dev_token() -> str | None:
    """读取项目根目录的 .dev_token 文件内容。"""
    try:
        token_path = Path(__file__).resolve().parent.parent.parent.parent / ".dev_token"
        if token_path.exists():
            return token_path.read_text().strip()
    except Exception:
        pass
    return None


def _extract_user(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw_token = auth[7:]

    # 开发环境：允许 .dev_token 永久 token 直接通过
    if _is_dev_env():
        dev_token = _load_dev_token()
        if dev_token and raw_token == dev_token:
            try:
                payload = decode_token(raw_token)
                return {"user_id": int(payload["sub"]), "tier": payload.get("tier", 0)}
            except Exception:
                pass

    try:
        payload = decode_token(raw_token)
        return {"user_id": int(payload["sub"]), "tier": payload.get("tier", 0)}
    except Exception as e:
        logger.debug(f"Token decode failed: {e}")
        return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _client_ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:512]


import logging as _logging
_logger = _logging.getLogger("auth_middleware")


async def require_auth(request: Request) -> dict:
    """强制鉴权：从 Bearer token 提取用户，未登录抛出 401，同时记录访问日志。"""
    _logger.debug(f"require_auth called, headers: {dict(request.headers)}")
    user = _extract_user(request)
    if user is None:
        _logger.warning(f"Auth failed for {request.url.path}")
        raise HTTPException(status_code=401, detail="请先登录")
    _logger.info(f"Auth OK: user_id={user['user_id']} for {request.url.path}")
    log_access(user["user_id"], str(request.url.path), _client_ip(request), _client_ua(request))
    return user


async def require_auth_optional(request: Request) -> dict | None:
    """可选鉴权：提取用户但不强制，同时记录访问日志（未登录记录 user_id=null）。"""
    user = _extract_user(request)
    uid = user["user_id"] if user else None
    log_access(uid, str(request.url.path), _client_ip(request), _client_ua(request))
    return user
