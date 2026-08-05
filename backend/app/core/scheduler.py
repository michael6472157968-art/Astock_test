"""APScheduler 定时任务调度——与 FastAPI 同进程运行。

收盘后自动触发：日线同步 → 选股池 → 板块分析 → 复盘 → 风险清单
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("scheduler")

TZ = "Asia/Shanghai"
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone=TZ)

    _scheduler.add_job(
        _sync_daily_wrapper,
        CronTrigger(hour=16, minute=5, timezone=TZ),
        id="sync_daily",
        name="日线数据同步",
        replace_existing=True,
    )
    _scheduler.add_job(
        _sync_sector_wrapper,
        CronTrigger(hour=11, minute=35, timezone=TZ),
        id="sync_sector_am",
        name="盘中板块更新",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(f"Scheduler started ({len(_scheduler.get_jobs())} jobs)")


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler shutdown")


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


# ── 包装器（APScheduler在线程池中运行，需新建event loop）──

def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    except Exception as e:
        logger.exception(f"Scheduled task failed: {e}")
    finally:
        loop.close()


def _sync_daily_wrapper():
    logger.info("Scheduled: sync_daily_all starting")
    _run_async(_sync_daily_all())


async def _sync_daily_all():
    """所有收盘后同步+计算合并到一个event loop中执行，避免跨loop连接泄漏。"""
    from app.services.data_sync import sync_daily_data, sync_limit_list, sync_margin, sync_stock_basic, sync_daily_basic, sync_moneyflow_hsgt
    from app.services.stock_pool_engine import StockPoolEngine
    from app.services.short_term_engine import ShortTermEngine
    from app.services.sector_analysis import SectorAnalysisEngine
    from app.services.market_review import MarketReviewEngine
    from app.services.risk_scanner import RiskScanner
    from app.api.credits import settle_market_guesses
    import logging
    log = logging.getLogger("sync")
    for name, fn in [
        ("stock_basic", sync_stock_basic),
        ("daily_data", sync_daily_data),
        ("daily_basic", sync_daily_basic),
        ("limit_list", sync_limit_list),
        ("margin", sync_margin),
        ("moneyflow_hsgt", sync_moneyflow_hsgt),
    ]:
        try:
            n = await fn()
            if isinstance(n, int):
                log.info(f"sync_{name}: {n} records")
        except Exception as e:
            log.exception(f"sync_{name} failed: {e}")

    log.info("scan: personalized alerts")
    try:
        from app.services.alert_engine import AlertEngine
        await AlertEngine().scan_all(trade_date)
    except Exception as e:
        log.exception(f"alert scan failed: {e}")

    log.info("compute: stock pool engines")
    try:
        await StockPoolEngine().compute_all()
        await ShortTermEngine().compute_all()
        await SectorAnalysisEngine().compute_all()
        await MarketReviewEngine().compute()
        scanner = RiskScanner()
        await scanner.scan_risk_list()
    except Exception as e:
        log.exception(f"compute engines failed: {e}")

    log.info("settle: market guesses")
    try:
        await settle_market_guesses()
    except Exception as e:
        log.exception(f"settle guesses failed: {e}")


def _sync_sector_wrapper():
    from app.services.data_sync import sync_sector_data
    logger.info("Scheduled: sync_sector_data")
    _run_async(sync_sector_data())
