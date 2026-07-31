"""统一异常定义与全局处理。

所有业务异常继承 AppError，全局 handler 统一转换为 APIResponse 格式。
"""

from __future__ import annotations

import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app")


class AppError(Exception):
    """业务异常基类。"""
    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code


class StockNotFoundError(AppError):
    def __init__(self, stock_code: str):
        super().__init__(f"股票 {stock_code} 无数据", 404)


class TushareServiceError(AppError):
    def __init__(self, message: str):
        super().__init__(f"数据服务暂不可用: {message}", 502)


class TushareQuotaError(AppError):
    def __init__(self, message: str):
        super().__init__(f"Tushare额度已用尽: {message}", 429)


class AuthError(AppError):
    def __init__(self, message: str):
        super().__init__(message, 401)


class TierDeniedError(AppError):
    def __init__(self, message: str = "当前用户等级无权限访问此功能"):
        super().__init__(message, 403)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={
            "code": exc.code,
            "message": exc.message,
            "data": None,
            "timestamp": int(time.time()),
            "ext_info": {},
        },
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "data": None,
            "timestamp": int(time.time()),
            "ext_info": {},
        },
    )
