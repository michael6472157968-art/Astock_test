"""SQLite 数据库初始化——aiosqlite异步引擎。

首次启动自动建表，零手动操作。
Alembic 用于增量迁移，create_all 用于全新安装。
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import get_settings

logger = logging.getLogger("db")

_settings = get_settings()
engine = create_async_engine(
    _settings.database_url,
    echo=False,
    connect_args={"timeout": 30},  # 等待而非立即报 locked 错误
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode for better concurrent read/write performance."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """创建所有表并运行 Alembic 迁移。"""
    from app.models.orm.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 存量数据库补充 industry 索引
        await conn.run_sync(lambda sync_conn: sync_conn.execute(
            __import__("sqlalchemy").text(
                "CREATE INDEX IF NOT EXISTS ix_stocks_industry ON stocks (industry)"
            )
        ))

        # 自动补全旧数据库缺失的列（create_all 只建新表不改旧表）
        result = await conn.execute(
            __import__("sqlalchemy").text("PRAGMA table_info('users')")
        )
        existing = {row[1] for row in await result.fetchall()}
        upgrades = {
            "credits": "ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 0",
            "is_active": "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        }
        for col, sql in upgrades.items():
            if col not in existing:
                await conn.execute(__import__("sqlalchemy").text(sql))
                logger.info(f"Schema upgrade: added users.{col}")

    logger.info("SQLite tables created (migrations run via entrypoint)")
