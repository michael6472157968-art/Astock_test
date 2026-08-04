"""数据同步服务——从Tushare拉取数据写入SQLite。

定时任务触发：收盘后同步日线、板块、财报。
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.database import async_session
from app.models.orm.models import LimitListRecord, MarginRecord, Sector, SectorDaily, Stock, StockDaily, StockFinancial
from app.services.tushare_client import (call_tushare, get_all_daily, get_limit_list, get_margin,
                                          get_sector_list, get_stock_basic)

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
    """同步指定交易日全市场日线。默认最近交易日。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

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


async def sync_financials() -> int:
    """同步全市场最新财报——月度任务，需控制频率。

    注意：此函数暂未启用。stock_financials 表存在但无任何代码查询。
    未来实现基本面分析（PE/PB/ROE）时，在 scheduler.py 中重新启用
    _sync_financials_wrapper 任务并扩展此函数覆盖全市场 + 三张表。
    """
    stocks = await get_stock_basic()
    if not stocks:
        logger.warning("No stock list available for financial sync")
        return 0

    count = 0
    for stock in stocks[:50]:  # 月度仅同步前50只关键股票，避免超额
        ts_code = stock.get("ts_code", "")
        try:
            income = await call_tushare("income", call_type="financial", ts_code=ts_code)
            if income is not None and not (hasattr(income, 'empty') and income.empty):
                async with async_session() as session:
                    session.add(StockFinancial(
                        ts_code=ts_code,
                        report_date=str(income.iloc[0].get("end_date", "")),
                        report_type="income",
                        data_json=str(income.iloc[0].to_dict()),
                    ))
                    await session.commit()
            count += 1
        except Exception as e:
            logger.warning(f"Financial sync failed for {ts_code}: {e}")
            continue

    logger.info(f"Financials synced: {count} stocks")
    return count


async def sync_limit_list(trade_date: str = "") -> int:
    """同步涨跌停列表（标准 limit_list，120积分）。写入 limit_list_records 表。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    rows = await get_limit_list(trade_date)

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
