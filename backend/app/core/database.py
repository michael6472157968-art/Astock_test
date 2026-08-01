"""SQLite 数据库初始化——aiosqlite异步引擎。

首次启动自动建表，零手动操作。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import get_settings

logger = logging.getLogger("db")

_settings = get_settings()
engine = create_async_engine(_settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """创建所有表。生产环境应使用Alembic迁移。"""
    from app.models.orm.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("SQLite tables created")

    _run_migrations()


def _run_migrations() -> None:
    """同步执行数据库迁移（sqlite3直连，与risk_scanner模式一致）。"""
    import sqlite3

    db_path = os.path.join(_settings.data_dir, "stock_analyzer.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", db_path)
    db_path = os.path.normpath(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 001: 清理 stock_daily 重复行 + 唯一索引 + trade_date 索引
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_stock_daily_ts_code_trade_date_unique'")
    if not cur.fetchone():
        cur.execute("""
            DELETE FROM stock_daily WHERE id NOT IN (
                SELECT MIN(id) FROM stock_daily GROUP BY ts_code, trade_date
            )
        """)
        deleted = cur.rowcount
        logger.info(f"Migration 001: deleted {deleted} duplicate stock_daily rows")

        cur.execute("""
            CREATE UNIQUE INDEX ix_stock_daily_ts_code_trade_date_unique
            ON stock_daily (ts_code, trade_date)
        """)
        logger.info("Migration 001: created unique index on (ts_code, trade_date)")

    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_stock_daily_trade_date'")
    if not cur.fetchone():
        cur.execute("CREATE INDEX ix_stock_daily_trade_date ON stock_daily (trade_date)")
        logger.info("Migration 001: created index on trade_date")

    conn.commit()
    conn.close()
