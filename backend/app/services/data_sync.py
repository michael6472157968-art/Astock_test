"""数据同步服务——从Tushare拉取数据写入SQLite。

定时任务触发：收盘后同步日线、板块、财报。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

from app.core.database import async_session
from app.models.orm.models import LimitListRecord, MarginRecord, Sector, Stock, StockDaily
from app.services.tushare_client import (get_all_daily, get_broker_recommend, get_cyq_perf, get_daily_basic,
                                          get_dc_index, get_express, get_hsgt_top10, get_index_daily, get_limit_list,
                                          get_margin, get_moneyflow, get_moneyflow_hsgt, get_sector_list, get_share_float,
                                          get_stock_basic, get_stk_holdertrade, get_stk_holdernumber, get_top_inst,
                                          get_top_list, get_top10_floatholders)

logger = logging.getLogger("sync")

# 4大指数——同步到 stock_daily，供日历着色等使用
INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]


async def sync_index_daily(trade_date: str = "") -> int:
    """同步4大指数日线到 stock_daily 表。每次调4次 index_daily API。

    若 trade_date 为空，默认取最新交易日（每日调度场景）。
    否则按指定日期同步（历史回填场景）。
    """
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    total = await _sync_index_daily_for_date(trade_date)
    logger.info(f"Index daily synced: {total} records for {trade_date}")
    return total


async def _sync_index_daily_for_date(trade_date: str) -> int:
    """为单日同步4大指数（内部调用，不重新解析 trade_date）。"""
    total = 0
    for code, _name in INDEX_CODES:
        try:
            rows = await get_index_daily(code, trade_date, trade_date)
            if not rows:
                continue
            async with async_session() as session:
                for row in rows:
                    try:
                        await session.execute(text("""
                            INSERT OR REPLACE INTO stock_daily
                                (ts_code, trade_date, open, high, low, close, pre_close,
                                 change, pct_chg, volume, amount)
                            VALUES (:ts_code, :trade_date, :open, :high, :low, :close,
                                    :pre_close, :change, :pct_chg, :volume, :amount)
                        """), {
                            "ts_code": code,
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
                        total += 1
                    except Exception:
                        continue
                await session.commit()
        except Exception as e:
            logger.warning(f"sync_index_daily {code}: {e}")
    return total


async def sync_index_historical(days: int = 120) -> int:
    """回填历史指数日线到 stock_daily。

    与 sync_historical_daily 配合使用，
    确保 calendar/market 等页面有足够的历史指数数据。
    已有数据的日期自动跳过，不会重复消耗 Tushare 调用。
    """
    from datetime import date, timedelta
    import asyncio

    end = date.today()
    total = 0
    skipped = 0
    for offset in range(days):
        d_dt = end - timedelta(days=offset)
        d = d_dt.strftime("%Y%m%d")
        # 周末跳过——Tushare永远返回空，不必浪费API配额
        if d_dt.weekday() >= 5:
            skipped += 1
            continue
        existing = 0
        try:
            async with async_session() as sess:
                r = await sess.execute(
                    text("SELECT COUNT(*) FROM stock_daily WHERE ts_code = '000001.SH' AND trade_date = :d"),
                    {"d": d},
                )
                existing = r.scalar() or 0
        except Exception:
            pass
        if existing > 0:
            skipped += 1
            continue
        try:
            n = await _sync_index_daily_for_date(d)
            total += n
            if n > 0:
                logger.debug(f"Index historical {d}: {n} records")
        except Exception as e:
            logger.warning(f"Index historical {d}: {e}")
        # 每处理一批交易日稍息，避开 Tushare 分钟限频
        if offset % 20 == 19:
            await asyncio.sleep(1)
    logger.info(f"Index historical sync: {total} new records, {skipped} skipped (total {days} days)")
    return total


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


async def _insert_daily_rows(rows: list[dict]) -> int:
    """将全市场日线行写入 stock_daily（INSERT OR IGNORE），返回写入行数。"""
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
                    "trade_date": str(row.get("trade_date", "")),
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
    return len(rows)


async def sync_daily_data(trade_date: str = "") -> int:
    """同步个股日线。指定 trade_date 同步单日；否则回看最近10个交易日补齐 DB 缺失的日。

    历史 bug：原逻辑"往回找5天只同步第一个有数据的日"，配合 16:05 触发早于
    Tushare 日线更新时间(约17:30+)，最新交易日个股永远滞后。改为回看补齐后，
    即使某天触发时数据未更新，下次触发也会自动补上。
    """
    if trade_date:
        rows = await get_all_daily(trade_date)
        if not rows:
            logger.info(f"No daily data for {trade_date}")
            return 0
        await _insert_daily_rows(rows)
        logger.info(f"Daily data: {len(rows)} stocks for {trade_date}")
        return len(rows)

    from app.utils.trading_calendar import is_trade_date
    today = date.today()
    total = 0
    for offset in range(10):
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        if not await is_trade_date(td):
            continue
        # 已有个股数据(>100只)视为已同步，跳过，避免重复调 Tushare
        async with async_session() as s:
            r = await s.execute(text(
                "SELECT COUNT(*) FROM stock_daily WHERE trade_date = :td "
                "AND ts_code NOT IN ('000001.SH','000688.SH','399001.SZ','399006.SZ')"
            ), {"td": td})
            if (r.scalar() or 0) > 100:
                continue
        rows = await get_all_daily(td)
        if not rows:
            continue
        await _insert_daily_rows(rows)
        total += len(rows)
        logger.info(f"Daily data backfilled: {len(rows)} stocks for {td}")
    return total


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


async def _insert_daily_basic_rows(rows: list[dict]) -> int:
    """将每日指标行写入 daily_basic（INSERT OR REPLACE），返回写入行数。"""
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO daily_basic
                        (ts_code, trade_date, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm,
                         total_mv, circ_mv, turnover_rate)
                    VALUES (:ts, :td, :pe, :pet, :pb, :ps, :pst, :dvr, :dvt,
                            :tmv, :cmv, :tr)
                """), {
                    "ts": row.get("ts_code", ""),
                    "td": str(row.get("trade_date", "")),
                    "pe": float(row.get("pe", 0) or 0),
                    "pet": float(row.get("pe_ttm", 0) or 0),
                    "pb": float(row.get("pb", 0) or 0),
                    "ps": float(row.get("ps", 0) or 0),
                    "pst": float(row.get("ps_ttm", 0) or 0),
                    "dvr": float(row.get("dv_ratio", 0) or 0),
                    "dvt": float(row.get("dv_ttm", 0) or 0),
                    "tmv": float(row.get("total_mv", 0) or 0),
                    "cmv": float(row.get("circ_mv", 0) or 0),
                    "tr": float(row.get("turnover_rate", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()
    return len(rows)


async def sync_daily_basic(trade_date: str = "") -> int:
    """同步每日指标 PE/PB/市值/换手率。指定 trade_date 同步单日；否则回看最近10个交易日补齐缺失。"""
    if trade_date:
        rows = await get_daily_basic(trade_date)
        if not rows:
            logger.info(f"No daily_basic data for {trade_date}")
            return 0
        await _insert_daily_basic_rows(rows)
        logger.info(f"Daily_basic synced: {len(rows)} stocks for {trade_date}")
        return len(rows)

    from app.utils.trading_calendar import is_trade_date
    today = date.today()
    total = 0
    for offset in range(10):
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        if not await is_trade_date(td):
            continue
        async with async_session() as s:
            r = await s.execute(text(
                "SELECT COUNT(*) FROM daily_basic WHERE trade_date = :td"
            ), {"td": td})
            if (r.scalar() or 0) > 100:
                continue
        rows = await get_daily_basic(td)
        if not rows:
            continue
        await _insert_daily_basic_rows(rows)
        total += len(rows)
        logger.info(f"Daily_basic backfilled: {len(rows)} stocks for {td}")
    return total


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


async def sync_hsgt_top10(trade_date: str = "") -> int:
    """同步沪深港通十大成交股。2000积分解锁。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    rows = await get_hsgt_top10(trade_date)
    if not rows:
        logger.info(f"No hsgt_top10 data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO hsgt_top10
                        (trade_date, ts_code, name, close, change, rank,
                         market_type, amount, net_amount, buy, sell)
                    VALUES (:td, :ts, :nm, :cl, :ch, :rk, :mt, :am, :na, :by, :sl)
                """), {
                    "td": str(row.get("trade_date", trade_date)),
                    "ts": row.get("ts_code", ""),
                    "nm": row.get("name", ""),
                    "cl": float(row.get("close", 0) or 0),
                    "ch": float(row.get("change", 0) or 0),
                    "rk": int(row.get("rank", 0) or 0),
                    "mt": str(row.get("market_type", "")),
                    "am": float(row.get("amount", 0) or 0),
                    "na": float(row.get("net_amount", 0) or 0),
                    "by": float(row.get("buy", 0) or 0),
                    "sl": float(row.get("sell", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Hsgt_top10 synced: {len(rows)} records for {trade_date}")
    return len(rows)


async def sync_moneyflow(trade_date: str = "") -> int:
    """同步个股资金流向——按交易日全市场一次API调用。金额单位万元。

    写入 moneyflow_records 表。若 trade_date 为空，默认最新交易日。
    """
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()

    rows = await get_moneyflow("", trade_date, trade_date)
    if not rows:
        logger.info(f"No moneyflow data for {trade_date}")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO moneyflow_records
                        (ts_code, trade_date, net_mf_amount, net_mf_vol,
                         buy_elg_amount, sell_elg_amount, buy_lg_amount, sell_lg_amount,
                         buy_md_amount, sell_md_amount, buy_sm_amount, sell_sm_amount)
                    VALUES (:ts, :td, :nmf, :nmv, :belg, :selg, :blg, :slg,
                            :bmd, :smd, :bsm, :ssm)
                """), {
                    "ts": row.get("ts_code", ""),
                    "td": str(row.get("trade_date", trade_date)),
                    "nmf": float(row.get("net_mf_amount", 0) or 0),
                    "nmv": float(row.get("net_mf_vol", 0) or 0),
                    "belg": float(row.get("buy_elg_amount", 0) or 0),
                    "selg": float(row.get("sell_elg_amount", 0) or 0),
                    "blg": float(row.get("buy_lg_amount", 0) or 0),
                    "slg": float(row.get("sell_lg_amount", 0) or 0),
                    "bmd": float(row.get("buy_md_amount", 0) or 0),
                    "smd": float(row.get("sell_md_amount", 0) or 0),
                    "bsm": float(row.get("buy_sm_amount", 0) or 0),
                    "ssm": float(row.get("sell_sm_amount", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Moneyflow synced: {len(rows)} records for {trade_date}")
    return len(rows)


async def sync_moneyflow_historical(days: int = 500) -> int:
    """回填历史个股资金流向。逐交易日调用 sync_moneyflow，已有数据自动跳过。

    days 为回溯的日历天数（约 500 天覆盖 2 年交易日）。
    """
    from datetime import date, timedelta
    import asyncio

    end = date.today()
    start = (end - timedelta(days=days)).strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    from app.utils.trading_calendar import get_trade_days_in_range
    trade_days = await get_trade_days_in_range(start, end_str)

    total = 0
    skipped = 0
    for i, td in enumerate(trade_days):
        # 已有数据的日期跳过（避免重复消耗 API）
        try:
            async with async_session() as sess:
                r = await sess.execute(
                    text("SELECT COUNT(*) FROM moneyflow_records WHERE trade_date = :d"),
                    {"d": td},
                )
                if (r.scalar() or 0) > 0:
                    skipped += 1
                    continue
        except Exception:
            pass
        try:
            n = await sync_moneyflow(td)
            total += n
        except Exception as e:
            logger.warning(f"Moneyflow historical {td}: {e}")
        # 每 20 个交易日稍息，避开分钟限频
        if i % 20 == 19:
            await asyncio.sleep(1)

    logger.info(f"Moneyflow historical sync: {total} new records, {skipped} skipped "
                f"(total {len(trade_days)} trade days)")
    return total


async def sync_top10_floatholders() -> int:
    """同步十大流通股东——取最新报告期全市场数据。2000积分解锁。"""
    rows = await get_top10_floatholders()
    if not rows:
        logger.info("No top10_floatholders data")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO top10_floatholders
                        (ts_code, ann_date, end_date, holder_name, hold_amount,
                         hold_ratio, hold_float_ratio, hold_change, holder_type)
                    VALUES (:ts, :ad, :ed, :hn, :ha, :hr, :fr, :hc, :ht)
                """), {
                    "ts": row.get("ts_code", ""),
                    "ad": str(row.get("ann_date", "")),
                    "ed": str(row.get("end_date", "")),
                    "hn": str(row.get("holder_name", "")),
                    "ha": float(row.get("hold_amount", 0) or 0),
                    "hr": float(row.get("hold_ratio", 0) or 0),
                    "fr": float(row.get("hold_float_ratio", 0) or 0),
                    "hc": float(row.get("hold_change", 0) or 0),
                    "ht": str(row.get("holder_type", "")),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Top10_floatholders synced: {len(rows)} records")
    return len(rows)


async def sync_stk_holdernumber() -> int:
    """同步股东户数——全市场最新报告期。2000积分解锁。"""
    rows = await get_stk_holdernumber()
    if not rows:
        logger.info("No stk_holdernumber data")
        return 0

    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO stk_holdernumber
                        (ts_code, ann_date, end_date, holder_num)
                    VALUES (:ts, :ad, :ed, :hn)
                """), {
                    "ts": row.get("ts_code", ""),
                    "ad": str(row.get("ann_date", "")),
                    "ed": str(row.get("end_date", "")),
                    "hn": int(row.get("holder_num", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()

    logger.info(f"Stk_holdernumber synced: {len(rows)} records")
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
            if cnt and cnt >= 5500:  # 该日期已有全市场数据，跳过
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


# ════════════════════════ 8000积分特色数据同步 ════════════════════════

async def sync_cyq_perf(trade_date: str = "") -> int:
    """同步筹码及胜率——全市场单日。8000积分解锁。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()
    rows = await get_cyq_perf(trade_date)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO cyq_perf
                        (ts_code, trade_date, his_low, his_high, cost_5pct, cost_15pct, cost_50pct,
                         cost_85pct, cost_95pct, weight_avg, winner_rate)
                    VALUES (:ts, :td, :hl, :hh, :c5, :c15, :c50, :c85, :c95, :wa, :wr)
                """), {
                    "ts": row.get("ts_code", ""), "td": str(row.get("trade_date", trade_date)),
                    "hl": row.get("his_low"), "hh": row.get("his_high"),
                    "c5": row.get("cost_5pct"), "c15": row.get("cost_15pct"), "c50": row.get("cost_50pct"),
                    "c85": row.get("cost_85pct"), "c95": row.get("cost_95pct"),
                    "wa": row.get("weight_avg"), "wr": row.get("winner_rate"),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Cyq_perf synced: {len(rows)} for {trade_date}")
    return len(rows)


async def sync_top_list(trade_date: str = "") -> int:
    """同步龙虎榜每日明细。5000积分解锁。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()
    rows = await get_top_list(trade_date)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO top_list
                        (trade_date, ts_code, name, close, pct_change, turnover_rate, amount,
                         l_sell, l_buy, l_amount, net_amount, net_rate, amount_rate, float_values, reason)
                    VALUES (:td, :ts, :nm, :cl, :pc, :tr, :am, :ls, :lb, :la, :na, :nr, :ar, :fv, :rs)
                """), {
                    "td": str(row.get("trade_date", trade_date)), "ts": row.get("ts_code", ""),
                    "nm": row.get("name", ""), "cl": row.get("close"), "pc": row.get("pct_change"),
                    "tr": row.get("turnover_rate"), "am": row.get("amount"),
                    "ls": row.get("l_sell"), "lb": row.get("l_buy"), "la": row.get("l_amount"),
                    "na": row.get("net_amount"), "nr": row.get("net_rate"), "ar": row.get("amount_rate"),
                    "fv": row.get("float_values"), "rs": row.get("reason"),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Top_list synced: {len(rows)} for {trade_date}")
    return len(rows)


async def sync_top_inst(trade_date: str = "") -> int:
    """同步龙虎榜机构交易单。5000积分解锁。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()
    rows = await get_top_inst(trade_date)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO top_inst
                        (trade_date, ts_code, exalter, side, buy, buy_rate, sell, sell_rate, net_buy, reason)
                    VALUES (:td, :ts, :ex, :sd, :by, :br, :sl, :sr, :nb, :rs)
                """), {
                    "td": str(row.get("trade_date", trade_date)), "ts": row.get("ts_code", ""),
                    "ex": row.get("exalter", ""), "sd": row.get("side", ""),
                    "by": row.get("buy"), "br": row.get("buy_rate"),
                    "sl": row.get("sell"), "sr": row.get("sell_rate"),
                    "nb": row.get("net_buy"), "rs": row.get("reason"),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Top_inst synced: {len(rows)} for {trade_date}")
    return len(rows)


async def sync_dc_index(trade_date: str = "") -> int:
    """同步东财概念/行业板块。6000积分解锁。"""
    if not trade_date:
        from app.utils.trading_calendar import get_latest_trade_date
        trade_date = await get_latest_trade_date()
    total = 0
    for idx_type in ["概念板块", "行业板块"]:
        rows = await get_dc_index(trade_date, idx_type)
        if not rows:
            continue
        async with async_session() as session:
            for row in rows:
                try:
                    await session.execute(text("""
                        INSERT OR REPLACE INTO dc_index
                            (trade_date, ts_code, name, leading, leading_code, pct_change, leading_pct,
                             total_mv, turnover_rate, up_num, down_num, idx_type, level)
                        VALUES (:td, :ts, :nm, :ld, :lc, :pc, :lp, :tm, :tr, :un, :dn, :it, :lv)
                    """), {
                        "td": str(row.get("trade_date", trade_date)), "ts": row.get("ts_code", ""),
                        "nm": row.get("name", ""), "ld": row.get("leading", ""), "lc": row.get("leading_code", ""),
                        "pc": row.get("pct_change"), "lp": row.get("leading_pct"),
                        "tm": row.get("total_mv"), "tr": row.get("turnover_rate"),
                        "un": row.get("up_num"), "dn": row.get("down_num"),
                        "it": idx_type, "lv": row.get("level"),
                    })
                except Exception:
                    continue
            await session.commit()
        total += len(rows)
    logger.info(f"Dc_index synced: {total} for {trade_date}")
    return total


async def sync_broker_recommend(month: str = "") -> int:
    """同步券商月度金股。6000积分解锁。"""
    if not month:
        from datetime import date
        month = date.today().strftime("%Y%m")
    rows = await get_broker_recommend(month)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO broker_recommend (month, broker, ts_code, name)
                    VALUES (:m, :br, :ts, :nm)
                """), {
                    "m": str(row.get("month", month)), "br": row.get("broker", ""),
                    "ts": row.get("ts_code", ""), "nm": row.get("name", ""),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Broker_recommend synced: {len(rows)} for {month}")
    return len(rows)


async def sync_share_float() -> int:
    """同步限售股解禁——未来30天。5000积分解锁。"""
    from datetime import date, timedelta
    start = date.today().strftime("%Y%m%d")
    end = (date.today() + timedelta(days=60)).strftime("%Y%m%d")
    rows = await get_share_float(start, end)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO share_float
                        (ts_code, ann_date, float_date, float_share, float_ratio, holder_name, share_type)
                    VALUES (:ts, :ad, :fd, :fs, :fr, :hn, :st)
                """), {
                    "ts": row.get("ts_code", ""), "ad": str(row.get("ann_date", "")),
                    "fd": str(row.get("float_date", "")), "fs": row.get("float_share"),
                    "fr": row.get("float_ratio"), "hn": row.get("holder_name", ""), "st": row.get("share_type", ""),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Share_float synced: {len(rows)}")
    return len(rows)


async def sync_stk_holdertrade() -> int:
    """同步股东增减持——近30天。5000积分解锁。"""
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    rows = await get_stk_holdertrade(start, end)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO stk_holdertrade
                        (ts_code, ann_date, holder_name, holder_type, in_de, change_vol, change_ratio,
                         after_share, after_ratio, avg_price, total_share)
                    VALUES (:ts, :ad, :hn, :ht, :id, :cv, :cr, :as_, :ar, :ap, :tsh)
                """), {
                    "ts": row.get("ts_code", ""), "ad": str(row.get("ann_date", "")),
                    "hn": row.get("holder_name", ""), "ht": row.get("holder_type", ""),
                    "id": row.get("in_de", ""), "cv": row.get("change_vol"), "cr": row.get("change_ratio"),
                    "as_": row.get("after_share"), "ar": row.get("after_ratio"),
                    "ap": row.get("avg_price"), "tsh": row.get("total_share"),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Stk_holdertrade synced: {len(rows)}")
    return len(rows)


async def sync_express(period: str = "") -> int:
    """同步业绩快报。2000积分解锁。"""
    if not period:
        from datetime import date
        y = date.today().year
        period = f"{y}0630"
    rows = await get_express(period)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO express
                        (ts_code, ann_date, end_date, revenue, operate_profit, total_profit, n_income,
                         yoy_net_profit, yoy_sales, yoy_op)
                    VALUES (:ts, :ad, :ed, :rv, :op, :tp, :ni, :ynp, :ys, :yo)
                """), {
                    "ts": row.get("ts_code", ""), "ad": str(row.get("ann_date", "")),
                    "ed": str(row.get("end_date", "")), "rv": row.get("revenue"),
                    "op": row.get("operate_profit"), "tp": row.get("total_profit"), "ni": row.get("n_income"),
                    "ynp": row.get("yoy_net_profit"), "ys": row.get("yoy_sales"), "yo": row.get("yoy_op"),
                })
            except Exception:
                continue
        await session.commit()
    logger.info(f"Express synced: {len(rows)} for {period}")
    return len(rows)
