"""访问日志——写入 access_logs 表，不阻塞主请求。"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import async_session
from app.models.orm.models import AccessLog

logger = logging.getLogger("access")


async def _write_log(user_id: int | None, endpoint: str, ip: str, ua: str) -> None:
    try:
        async with async_session() as session:
            session.add(AccessLog(
                user_id=user_id,
                endpoint=endpoint,
                ip_address=ip[:45] if ip else "",
                user_agent=ua[:512] if ua else "",
            ))
            await session.commit()
    except Exception:
        logger.warning("Access log write failed", exc_info=True)


def log_access(user_id: int | None, endpoint: str, ip: str, ua: str) -> None:
    """Fire-and-forget 异步写入访问日志。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write_log(user_id, endpoint, ip, ua))
    except RuntimeError:
        pass
