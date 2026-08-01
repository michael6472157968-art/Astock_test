"""SQLite 数据库初始化——aiosqlite异步引擎。

首次启动自动建表，零手动操作。
Alembic 用于增量迁移，create_all 用于全新安装。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import get_settings

logger = logging.getLogger("db")

_settings = get_settings()
engine = create_async_engine(_settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """创建所有表并运行 Alembic 迁移。"""
    from app.models.orm.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("SQLite tables created (migrations run via entrypoint)")
