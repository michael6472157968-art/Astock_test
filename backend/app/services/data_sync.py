"""数据同步服务——从Tushare拉取数据写入SQLite。

定时任务触发：收盘后同步日线、板块、财报。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

from app.core.database import async_session
from app.models.orm.models import LimitListRecord, MarginRecord, Sector, Stock, StockDaily
from app.services.tushare_client import (get_all_daily, get_daily_basic, get_limit_list, get_margin,
                                          get_moneyflow_hsgt, get_sector_list, get_stock_basic)

logger = logging.getLogger("sync")


async def sync_stock_basic() -> int:
    """同步股票基础信息。返回新增/更新数量。"""
    from app.core.cache import cache_delete
    await cache_delete("stock:basic:all")  # 清除永久缓存，强制从Tushare拉取最新

    stocks = await get_stock_basic()
    if not stocks:
        return 0

    async with async_session() as session:
        for row in stocks:
            try:
                await session.merge(Stock(
                    ts_code=row.get("ts_code", ""),
                    symbol=row.get("symbol", ""),
                    name=row.get("name", ""),
                    industry=row.get("industry", "") or "",
                    area=row.get("area", "") or "",
                    market=row.get("market", "") or "",
                    list_date=row.get("list_date", "") or "",
                ))
            except Exception:
                continue
        await session.commit()

    logger.info(f"Stock basic: {len(stocks)} records synced")
    return len(stocks)


async def sync_daily_data(trade_date: str = "") -> int:
    """同步指定交易日全市场日线。默认向前搜索拉到最新有数据的交易日。"""
    if not trade_date:
        from app.utils.trading_calendar import is_trade_date
        today = date.today()
        # 从今天往回找最近5天内的交易日，找到有数据就拉
        for offset in range(5):
            td = (today - timedelta(days=offset)).strftime("%Y%m%d")
            if not await is_trade_date(td):
                continue
            rows = await get_all_daily(td)
            if rows:
                trade_date = td
                break
        else:
            # fallback: use DB MAX
            from app.utils.trading_calendar import get_latest_trade_date
            trade_date = await get_latest_trade_date()
            rows = await get_all_daily(trade_date)
    else:
        rows = await get_all_daily(trade_date)

    if not rows:
        logger.info(f"No daily data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR IGNORE INTO stock_daily
                        (ts_code, trade_date, open, high, low, close, pre_close,
                         change, pct_chg, volume, amount)
                    VALUES (:ts_code, :trade_date, :open, :high, :low, :close,
                            :pre_close, :change, :pct_chg, :volume, :amount)
                """), {
                    "ts_code": row.get("ts_code", ""),
                    "trade_date": str(row.get("trade_date", trade_date)),
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "pre_close": float(row.get("pre_close", 0) or 0),
                    "change": float(row.get("change", 0) or 0),
                    "pct_chg": float(row.get("pct_chg", 0) or 0),
                    "volume": float(row.get("vol", 0) or 0),
                    "amount": float(row.get("amount", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Daily data: {len(rows)} stocks for {trade_date}")
    return len(rows)


async def sync_sector_data() -> int:
    """同步板块分类列表。"""
    sectors = await get_sector_list()
    if not sectors:
        return 0

    async with async_session() as session:
        for row in sectors:
            try:
                await session.merge(Sector(
                    code=row.get("index_code", ""),
                    name=row.get("industry_name", ""),
                    type=row.get("src", "SW2021"),
                ))
            except Exception:
                continue
        await session.commit()

    return len(sectors)


async def sync_limit_list(trade_date: str = "") -> int:
    """同步涨跌停列表。调用Tushare标准 limit_list 接口(120积分需要注册后社区贡献)。
    如接口返回"请指定正确的接口名"说明积分不足，静默跳过不阻断batch。
    """
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    try:
        rows = await get_limit_list(trade_date)
    except Exception as e:
        logger.warning(f"limit_list 接口暂不可用(积分不够?): {e}")
        return -1

    if not rows:
        logger.info(f"No limit list data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO limit_list_records
                        (trade_date, ts_code, name, price, pct_chg, limit_type, open_num, lu_desc, tag, status)
                    VALUES (:td, :ts, :nm, :pr, :pct, :lt, :onm, :ld, :tg, :st)
                """), {
                    "td": str(row.get("trade_date", trade_date)),
                    "ts": row.get("ts_code", ""),
                    "nm": row.get("name", ""),
                    "pr": float(row.get("close", 0) or 0),
                    "pct": float(row.get("pct_chg", 0) or 0),
                    "lt": str(row.get("limit", "")),
                    "onm": int(row.get("open_times", 0) or 0),
                    "ld": str(row.get("up_stat", ""))[:200],
                    "tg": "",
                    "st": str(row.get("limit_times", ""))[:50],
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Limit list synced: {len(rows)} records for {trade_date}")
    return len(rows)


async def sync_margin(trade_date: str = "") -> int:
    """同步融资融券明细。写入 margin_records 表。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    rows = await get_margin(trade_date)
    if not rows:
        logger.info(f"No margin data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO margin_records
                        (trade_date, ts_code, name, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye)
                    VALUES (:td, :ts, :nm, :rzye, :rqye, :rzmre, :rqyl, :rzche, :rqchl, :rqmcl, :rzrqye)
                """), {
                    "td": str(row.get("trade_date", trade_date)),
                    "ts": row.get("ts_code", ""),
                    "nm": row.get("name", ""),
                    "rzye": float(row.get("rzye", 0) or 0),
                    "rqye": float(row.get("rqye", 0) or 0),
                    "rzmre": float(row.get("rzmre", 0) or 0),
                    "rqyl": float(row.get("rqyl", 0) or 0),
                    "rzche": float(row.get("rzche", 0) or 0),
                    "rqchl": float(row.get("rqchl", 0) or 0),
                    "rqmcl": float(row.get("rqmcl", 0) or 0),
                    "rzrqye": float(row.get("rzrqye", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Margin synced: {len(rows)} records for {trade_date}")
    return len(rows)


async def sync_daily_basic(trade_date: str = "") -> int:
    """同步每日指标 PE/PB/市值/换手率。全市场一次API调用。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    rows = await get_daily_basic(trade_date)
    if not rows:
        logger.info(f"No daily_basic data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO daily_basic
                        (ts_code, trade_date, pe, pb, total_mv, circ_mv, turnover_rate)
                    VALUES (:ts, :td, :pe, :pb, :tmv, :cmv, :tr)
                """), {
                    "ts": row.get("ts_code", ""),
                    "td": str(row.get("trade_date", trade_date)),
                    "pe": float(row.get("pe", 0) or 0),
                    "pb": float(row.get("pb", 0) or 0),
                    "tmv": float(row.get("total_mv", 0) or 0),
                    "cmv": float(row.get("circ_mv", 0) or 0),
                    "tr": float(row.get("turnover_rate", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Daily_basic synced: {len(rows)} stocks for {trade_date}")
    return len(rows)


async def sync_moneyflow_hsgt(trade_date: str = "") -> int:
    """同步北向资金流向——一次API调全市场。写入 moneyflow_hsgt 表。"""
    if not trade_date:
        trade_date = date.today().strftime("%Y%m%d")

    end_d = trade_date
    start_d = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    rows = await get_moneyflow_hsgt(start_d, end_d)
    if not rows:
        logger.info(f"No moneyflow_hsgt data for {start_d}~{end_d}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO moneyflow_hsgt
                        (trade_date, north_money, south_money, ggt_ss, ggt_sz, hgt, sgt)
                    VALUES (:td, :nm, :sm, :gs, :gz, :hg, :sg)
                """), {
                    "td": str(row.get("trade_date", "")),
                    "nm": float(row.get("north_money", 0) or 0),
                    "sm": float(row.get("south_money", 0) or 0),
                    "gs": float(row.get("ggt_ss", 0) or 0),
                    "gz": float(row.get("ggt_sz", 0) or 0),
                    "hg": float(row.get("hgt", 0) or 0),
                    "sg": float(row.get("sgt", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Moneyflow_hsgt synced: {len(rows)} records")
    return len(rows)


async def sync_historical_daily(days: int = 60) -> dict:
    """启动时按日期补拉历史全市场日线——填补 stock_daily 历史缺口。

    每日期仅 1 次 Tushare API 调用（get_all_daily 覆盖全市场），
    60 天 <= 60 次调用，远低于 180/min 限制，不会触发 asyncio.sleep 限频冻结。
    已有数据的日期自动跳过。
    """
    from datetime import date, timedelta

    end = date.today()
    synced_dates = []
    skipped = 0

    for offset in range(days):
        d = (end - timedelta(days=offset)).strftime("%Y%m%d")
        async with async_session() as sess:
            r = await sess.execute(
                text("SELECT COUNT(*) FROM stock_daily WHERE trade_date = :d"),
                {"d": d},
            )
            cnt = r.scalar()
            if cnt and cnt >= 5000:  # 该日期已有全市场数据，跳过
                skipped += 1
                continue

        try:
            count = await sync_daily_data(d)
            if count > 0:
                synced_dates.append(d)
                logger.info(f"Historical sync: {d} ({count} stocks)")
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"Historical sync: {d} failed ({e})")
            skipped += 1

    logger.info(f"Historical sync complete: {len(synced_dates)} dates synced, {skipped} skipped")
    return {"synced": synced_dates, "skipped": skipped, "total_dates": days}
