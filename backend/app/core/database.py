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

        # stock_pool_results 查询去重去索引（按日期+类型+排名）
        await conn.run_sync(lambda sync_conn: sync_conn.execute(
            __import__("sqlalchemy").text(
                "CREATE INDEX IF NOT EXISTS ix_spr_calc_date_pool_rank ON stock_pool_results (calc_date, pool_type, rank_in_pool)"
            )
        ))

        # 自动补全旧数据库缺失的列（create_all 只建新表不改旧表）
        await _auto_migrate_schema(conn)

        # 防御性建表：确保新增的ORM表在存量数据库中也存在
        await _ensure_new_tables(conn)

        # 补建非 ORM 表(裸 SQL DDL)，解决全新部署缺表问题
        await _ensure_raw_tables(conn)

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


async def _ensure_new_tables(conn):
    """防御性补建存量DB缺失的ORM表（create_all 在某些环境下会跳过新表）。"""
    from sqlalchemy import text as _text
    from app.models.orm.models import Base

    for mapper in Base.registry.mappers:
        table = mapper.local_table
        result = await conn.execute(_text(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table.name}'"
        ))
        if not result.fetchone():
            await conn.run_sync(lambda sync_conn: table.create(sync_conn, checkfirst=True))
            logger.info(f"Schema upgrade: created table {table.name}")


# 非 ORM 表(用裸 SQL 直接建)的建表 DDL。
# 这些表历史上是脚本/一次性迁移建的,不在 models.py 里,create_all 不会建它们,
# 导致全新部署(如 Fly)时缺表。统一在这里补建,保证 fresh deploy 也完整。
_RAW_TABLE_DDL = [
    """CREATE TABLE IF NOT EXISTS broker_recommend (
        id INTEGER PRIMARY KEY AUTOINCREMENT, month VARCHAR(6) NOT NULL, broker VARCHAR(100),
        ts_code VARCHAR(20) NOT NULL, name VARCHAR(50),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(month, broker, ts_code))""",
    """CREATE TABLE IF NOT EXISTS cyq_perf (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, trade_date VARCHAR(8) NOT NULL,
        his_low REAL, his_high REAL, cost_5pct REAL, cost_15pct REAL, cost_50pct REAL,
        cost_85pct REAL, cost_95pct REAL, weight_avg REAL, winner_rate REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(ts_code, trade_date))""",
    """CREATE TABLE IF NOT EXISTS daily_reviews (
        id INTEGER NOT NULL, review_date VARCHAR(10) NOT NULL, content_json TEXT,
        created_at DATETIME, PRIMARY KEY (id), UNIQUE (review_date))""",
    """CREATE TABLE IF NOT EXISTS diagnosis_reports (
        id INTEGER NOT NULL, ts_code VARCHAR(20) NOT NULL, calc_date VARCHAR(10) NOT NULL,
        tech_score FLOAT, fundamental_score FLOAT, composite_score FLOAT, report_json TEXT,
        created_at DATETIME, PRIMARY KEY (id))""",
    """CREATE INDEX IF NOT EXISTS ix_diagnosis_reports_ts_code ON diagnosis_reports (ts_code)""",
    """CREATE TABLE IF NOT EXISTS express (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        end_date VARCHAR(8), revenue REAL, operate_profit REAL, total_profit REAL, n_income REAL,
        yoy_net_profit REAL, yoy_sales REAL, yoy_op REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(ts_code, end_date))""",
    """CREATE TABLE IF NOT EXISTS fina_indicator (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, end_date VARCHAR(8) NOT NULL,
        ann_date VARCHAR(8), cfps_yoy REAL, ocf_yoy REAL, ocfps REAL, ocf_to_debt REAL,
        dt_netprofit_yoy REAL, roe_yoy REAL, basic_eps_yoy REAL,
        roe REAL, roa REAL, grossprofit_margin REAL, netprofit_margin REAL,
        or_yoy REAL, netprofit_yoy REAL, debt_to_assets REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(ts_code, end_date))""",
    """CREATE TABLE IF NOT EXISTS hsgt_top10 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date VARCHAR(8) NOT NULL, ts_code VARCHAR(20) NOT NULL,
        name VARCHAR(50), close FLOAT, change FLOAT, rank INTEGER, market_type VARCHAR(5),
        amount FLOAT, net_amount FLOAT, buy FLOAT, sell FLOAT)""",
    """CREATE INDEX IF NOT EXISTS ix_hsgt_top10_td ON hsgt_top10(trade_date)""",
    """CREATE TABLE IF NOT EXISTS research_experiments (
        id INTEGER NOT NULL, experiment_id VARCHAR(30) NOT NULL, pool_name VARCHAR(30),
        config_hash VARCHAR(20), date_start VARCHAR(10), date_end VARCHAR(10), train_len INTEGER,
        test_len INTEGER, lookback INTEGER, ic_summary TEXT, group_result TEXT, status VARCHAR(20),
        created_at DATETIME, PRIMARY KEY (id))""",
    """CREATE INDEX IF NOT EXISTS ix_research_experiments_experiment_id ON research_experiments (experiment_id)""",
    """CREATE TABLE IF NOT EXISTS sector_daily (
        id INTEGER NOT NULL, code VARCHAR(20) NOT NULL, trade_date VARCHAR(8) NOT NULL,
        close FLOAT, pct_chg FLOAT, volume FLOAT, created_at DATETIME, PRIMARY KEY (id))""",
    """CREATE INDEX IF NOT EXISTS ix_sector_daily_code ON sector_daily (code)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_daily_code_date ON sector_daily(code, trade_date)""",
    """CREATE TABLE IF NOT EXISTS share_float (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        float_date VARCHAR(8), float_share REAL, float_ratio REAL, holder_name VARCHAR(100),
        share_type VARCHAR(50), created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS stk_holdernumber (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        end_date VARCHAR(8), holder_num INTEGER)""",
    """CREATE INDEX IF NOT EXISTS ix_holder_ts ON stk_holdernumber(ts_code)""",
    """CREATE INDEX IF NOT EXISTS ix_holder_ed ON stk_holdernumber(end_date)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ix_stk_holdernumber_ts_end ON stk_holdernumber (ts_code, end_date)""",
    """CREATE TABLE IF NOT EXISTS stk_holdertrade (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        holder_name VARCHAR(100), holder_type VARCHAR(10), in_de VARCHAR(10),
        change_vol REAL, change_ratio REAL, after_share REAL, after_ratio REAL,
        avg_price REAL, total_share REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS stock_financials (
        id INTEGER NOT NULL, ts_code VARCHAR(20) NOT NULL, report_date VARCHAR(10) NOT NULL,
        report_type VARCHAR(10), data_json TEXT, created_at DATETIME, PRIMARY KEY (id))""",
    """CREATE INDEX IF NOT EXISTS ix_stock_financials_ts_code ON stock_financials (ts_code)""",
    """CREATE TABLE IF NOT EXISTS top10_floatholders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        end_date VARCHAR(8), holder_name VARCHAR(100), hold_amount FLOAT, hold_ratio FLOAT,
        hold_float_ratio FLOAT, hold_change FLOAT, holder_type VARCHAR(50))""",
    """CREATE INDEX IF NOT EXISTS ix_float_ts ON top10_floatholders(ts_code)""",
    """CREATE INDEX IF NOT EXISTS ix_float_ed ON top10_floatholders(end_date)""",
    """CREATE TABLE IF NOT EXISTS top10_holders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code VARCHAR(20) NOT NULL, ann_date VARCHAR(8),
        end_date VARCHAR(8), holder_name VARCHAR(100), hold_amount REAL, hold_ratio REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS top_inst (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date VARCHAR(8) NOT NULL, ts_code VARCHAR(20) NOT NULL,
        exalter VARCHAR(100), side VARCHAR(10), buy REAL, buy_rate REAL, sell REAL, sell_rate REAL,
        net_buy REAL, reason VARCHAR(200), created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ix_top_inst_td_ts_ex ON top_inst (trade_date, ts_code, exalter)""",
    """CREATE TABLE IF NOT EXISTS top_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date VARCHAR(8) NOT NULL, ts_code VARCHAR(20) NOT NULL,
        name VARCHAR(50), close REAL, pct_change REAL, turnover_rate REAL, amount REAL,
        l_sell REAL, l_buy REAL, l_amount REAL, net_amount REAL, net_rate REAL,
        amount_rate REAL, float_values REAL, reason VARCHAR(200),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(trade_date, ts_code))""",
]


async def _ensure_raw_tables(conn):
    """补建非 ORM 表(裸 SQL DDL)，确保全新部署不缺表。"""
    from sqlalchemy import text as _text

    for ddl in _RAW_TABLE_DDL:
        try:
            await conn.execute(_text(ddl))
        except Exception as e:
            logger.warning(f"Raw table DDL failed: {str(e)[:80]}")
