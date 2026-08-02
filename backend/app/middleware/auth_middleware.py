from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from app.core.security import decode_token
from app.middleware.access_log import log_access

logger = logging.getLogger("auth_middleware")


def _extract_user(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        payload = decode_token(auth[7:])
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
