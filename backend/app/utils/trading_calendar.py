"""交易日历——从 Tushare trade_cal 获取最新交易日，缓存到 SQLite。

所有需要判断交易日的逻辑统一通过本模块，禁止在业务代码中自行判断。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger("trading_calendar")

_CALENDAR_CACHE_TABLE = "trading_calendar_cache"
_calendar_table_ready = False


async def ensure_calendar_table() -> None:
    """建表（仅 startup 调用一次），避免每次请求拿 SQLite 写锁。"""
    global _calendar_table_ready
    from app.core.database import async_session
    from sqlalchemy import text

    async with async_session() as sess:
        await sess.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {_CALENDAR_CACHE_TABLE} (
                cal_date TEXT PRIMARY KEY,
                is_open INTEGER NOT NULL DEFAULT 0
            )
        """))
        await sess.commit()
    _calendar_table_ready = True
    logger.info("Trading calendar cache table ensured")


async def _load_from_tushare(start_date: str, end_date: str) -> set[str]:
    """从 Tushare trade_cal 拉取交易日期并写入缓存表。返回该区间的交易日集合。"""
    from app.services.tushare_client import call_tushare

    df = await call_tushare("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
    if df is None or (hasattr(df, "empty") and df.empty):
        logger.warning("Tushare trade_cal returned empty")
        return set()

    trade_dates: set[str] = set()
    for _, row in df.iterrows():
        cal_date = str(row.get("cal_date", ""))
        is_open = int(row.get("is_open", 0))
        if cal_date:
            trade_dates.add(cal_date) if is_open == 1 else None

    if not trade_dates:
        return set()

    # 写入 SQLite 缓存
    from app.core.database import async_session
    from sqlalchemy import text

    async with async_session() as sess:
        for td in trade_dates:
            await sess.execute(
                text(f"INSERT OR IGNORE INTO {_CALENDAR_CACHE_TABLE} (cal_date, is_open) VALUES (:d, 1)"),
                {"d": td},
            )
        await sess.commit()

    logger.info(f"Trading calendar cached: {len(trade_dates)} days ({start_date} ~ {end_date})")
    return trade_dates


async def is_trade_date(date_str: str) -> bool:
    """判断指定日期是否为交易日（先查缓存，再调 Tushare）。"""
    from app.core.database import async_session
    from sqlalchemy import text

    async with async_session() as sess:
        r = await sess.execute(
            text(f"SELECT is_open FROM {_CALENDAR_CACHE_TABLE} WHERE cal_date = :d"),
            {"d": date_str},
        )
        row = r.first()
        if row is not None:
            return row[0] == 1

    # 缓存未命中，拉取该日期前后30天
    d = datetime.strptime(date_str, "%Y%m%d")
    start = (d - timedelta(days=30)).strftime("%Y%m%d")
    end = (d + timedelta(days=30)).strftime("%Y%m%d")
    trade_dates = await _load_from_tushare(start, end)
    return date_str in trade_dates


async def get_next_trade_date(from_date_str: str) -> str:
    """返回 from_date 当天或之后最近的一个交易日。

    向前搜索最多14天，找不到则抛 RuntimeError（仅极端休市期可能）。
    """
    from app.core.database import async_session
    from sqlalchemy import text

    d = datetime.strptime(from_date_str, "%Y%m%d")
    for offset in range(14):
        check = d + timedelta(days=offset)
        check_str = check.strftime("%Y%m%d")

        async with async_session() as sess:
            r = await sess.execute(
                text(f"SELECT is_open FROM {_CALENDAR_CACHE_TABLE} WHERE cal_date = :d"),
                {"d": check_str},
            )
            row = r.first()
            if row is not None:
                if row[0] == 1:
                    return check_str
                continue

        start = (check - timedelta(days=30)).strftime("%Y%m%d")
        end = (check + timedelta(days=30)).strftime("%Y%m%d")
        trade_dates = await _load_from_tushare(start, end)
        if check_str in trade_dates:
            return check_str

    raise RuntimeError(f"No trade day found within 14 days after {from_date_str}")


async def get_trade_days_in_range(start_date: str, end_date: str) -> list[str]:
    """返回 [start_date, end_date] 区间内所有交易日，按日期升序。"""
    from app.core.database import async_session
    from sqlalchemy import text

    async with async_session() as sess:
        r = await sess.execute(
            text(f"SELECT cal_date FROM {_CALENDAR_CACHE_TABLE} WHERE cal_date BETWEEN :s AND :e AND is_open=1 ORDER BY cal_date"),
            {"s": start_date, "e": end_date},
        )
        trade_days = [row[0] for row in r]

    # 缓存可能不完整（如只有零星几次 get_latest_trade_date 写入的条目），
    # 若不足区间预期交易日的 50%，触发 Tushare 补充拉取
    from datetime import datetime as _dt
    _days = (_dt.strptime(end_date, "%Y%m%d") - _dt.strptime(start_date, "%Y%m%d")).days + 1
    _expected = int(_days * 5 / 7 * 0.5)  # 周末约占 2/7，取预期一半为阈值
    if len(trade_days) < _expected:
        loaded = await _load_from_tushare(start_date, end_date)
        trade_days = sorted([d for d in loaded if start_date <= d <= end_date])

    return trade_days


async def get_latest_trade_date() -> str:
    """Return the most recent trade date that has actual stock_daily data.

    Falls back through: stock_daily MAX trade_date → trading calendar cache →
    Tushare trade_cal → weekday heuristic.
    """
    from app.core.database import async_session
    from sqlalchemy import text

    today_str = date.today().strftime("%Y%m%d")

    # 1. Check stock_daily for the newest trade date with data
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT MAX(trade_date) FROM stock_daily")
        )
        max_td = r.scalar()
        if max_td:
            return max_td
