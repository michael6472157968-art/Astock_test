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
        await _auto_migrate_schema(conn)

    logger.info("SQLite tables created (migrations run via entrypoint)")


async def _auto_migrate_schema(conn):
    """对比 ORM 模型与物理 SQLite 表，自动补全缺失列。

    SQLAlchemy create_all 只建新表不修改已有表结构，此函数确保存量数据库
    的列与 ORM 模型定义保持一致。
    """
    from sqlalchemy import text as _text
    from app.models.orm.models import Base

    # 收集所有 ORM 模型的 (table_name, column_name, column_type)
    orm_columns: dict[str, dict[str, str]] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table.name not in orm_columns:
            orm_columns[table.name] = {}
        for col in table.columns:
            sql_type = col.type.compile()
            default = col.default
            if default is not None and default.is_scalar and default.arg is not None:
                arg = default.arg
                if isinstance(arg, str):
                    arg = f"'{arg}'"
                sql_type += f" DEFAULT {arg}"
            orm_columns[table.name][col.name] = sql_type

    # 对每个 ORM 表检查实际 SQLite 列
    for table_name, expected_cols in orm_columns.items():
        result = await conn.execute(_text(f"PRAGMA table_info('{table_name}')"))
        rows = result.fetchall()
        if not rows:
            continue  # 表不存在，create_all 会处理
        existing = {row[1] for row in rows}
        for col_name in expected_cols:
            if col_name not in existing:
                sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {expected_cols[col_name]}"
                await conn.execute(_text(sql))
                logger.info(f"Schema upgrade: added {table_name}.{col_name}")
