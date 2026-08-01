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

    _run_alembic_migrations()
    logger.info("SQLite tables created and migrations complete")


def _run_alembic_migrations() -> None:
    """Run all pending Alembic migrations."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    import os as _os
    alembic_ini = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "..", "alembic.ini")
    alembic_ini = _os.path.normpath(alembic_ini)
    if not _os.path.exists(alembic_ini):
        logger.warning("alembic.ini not found, skipping migrations")
        return
    _cfg = AlembicConfig(alembic_ini)
    _cfg.set_main_option("sqlalchemy.url", _settings.database_url)
    command.upgrade(_cfg, "head")
    logger.info("Alembic migrations complete")
