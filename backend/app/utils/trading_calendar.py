"""交易日历——从 Tushare trade_cal 获取最新交易日，缓存到 SQLite。

所有需要判断交易日的逻辑统一通过本模块，禁止在业务代码中自行判断。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger("trading_calendar")

_CALENDAR_CACHE_TABLE = "trading_calendar_cache"


async def _ensure_cache_table() -> None:
    """确保交易日历缓存表存在。"""
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
    await _ensure_cache_table()

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


async def get_latest_trade_date() -> str:
    """获取最近一个交易日。

    - 如果今天是交易日且已收盘（15:30 后），返回今天
    - 如果今天是交易日但未收盘，返回上一交易日
    - 如果今天不是交易日，返回最近一个交易日
    """
    await _ensure_cache_table()

    today_str = date.today().strftime("%Y%m%d")
    today = datetime.now()

    # 向前扫描最多 10 天
    from app.core.database import async_session
    from sqlalchemy import text

    # 先从缓存查最近记录
    async with async_session() as sess:
        r = await sess.execute(
            text(f"SELECT cal_date FROM {_CALENDAR_CACHE_TABLE} WHERE is_open = 1 AND cal_date <= :d ORDER BY cal_date DESC LIMIT 1"),
            {"d": today_str},
        )
        row = r.first()

    if row:
        cached_date = row[0]
        # 如果今天在缓存中是交易日
        if cached_date == today_str:
            # 已收盘（15:30 后）→ 返回今天
            if today.hour >= 15 and today.minute >= 30:
                return today_str
            # 未收盘 → 返回上一个交易日
            async with async_session() as sess:
                r = await sess.execute(
                    text(f"SELECT cal_date FROM {_CALENDAR_CACHE_TABLE} WHERE is_open = 1 AND cal_date < :d ORDER BY cal_date DESC LIMIT 1"),
                    {"d": today_str},
                )
                prev = r.first()
                if prev:
                    return prev[0]
        return cached_date

    # 缓存无数据，拉取
    start = (today - timedelta(days=60)).strftime("%Y%m%d")
    end = today_str
    await _load_from_tushare(start, end)

    # 重查
    async with async_session() as sess:
        r = await sess.execute(
            text(f"SELECT cal_date FROM {_CALENDAR_CACHE_TABLE} WHERE is_open = 1 AND cal_date <= :d ORDER BY cal_date DESC LIMIT 1"),
            {"d": today_str},
        )
        row = r.first()

    if row:
        trade_date = row[0]
        if trade_date == today_str and not (today.hour >= 15 and today.minute >= 30):
            async with async_session() as sess:
                r = await sess.execute(
                    text(f"SELECT cal_date FROM {_CALENDAR_CACHE_TABLE} WHERE is_open = 1 AND cal_date < :d ORDER BY cal_date DESC LIMIT 1"),
                    {"d": today_str},
                )
                prev = r.first()
                if prev:
                    return prev[0]
        return trade_date

    # 彻底兜底：回退到周五（如果今天在周末）
    fallback = today
    while fallback.weekday() >= 5:
        fallback = fallback - timedelta(days=1)
    return fallback.strftime("%Y%m%d")
