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
        CronTrigger(hour=15, minute=35, timezone=TZ),
        id="sync_daily",
        name="日线数据同步",
        replace_existing=True,
    )
    _scheduler.add_job(
        _compute_engines_wrapper,
        CronTrigger(hour=15, minute=40, timezone=TZ),
        id="compute_engines",
        name="离线计算引擎",
        replace_existing=True,
    )
    _scheduler.add_job(
        _settle_guesses_wrapper,
        CronTrigger(hour=15, minute=45, timezone=TZ),
        id="settle_guesses",
        name="竞猜结算",
        replace_existing=True,
    )
    _scheduler.add_job(
        _sync_sector_wrapper,
        CronTrigger(hour=11, minute=35, timezone=TZ),
        id="sync_sector_am",
        name="盘中板块更新",
        replace_existing=True,
    )
    # ── 月度财报同步已禁用（stock_financials 表暂无代码查询，未来基本面分析启用时取消注释）──
    # _scheduler.add_job(
    #     _sync_financials_wrapper,
    #     CronTrigger(day=1, hour=5, minute=0, timezone=TZ),
    #     id="sync_financials",
    #     name="月度财报同步",
    #     replace_existing=True,
    # )

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
    from app.services.data_sync import sync_daily_data, sync_limit_list, sync_margin
    logger.info("Scheduled: sync_daily_data + limit_list + margin")
    _run_async(sync_daily_data())
    _run_async(sync_limit_list())
    _run_async(sync_margin())


def _sync_sector_wrapper():
    from app.services.data_sync import sync_sector_data
    logger.info("Scheduled: sync_sector_data")
    _run_async(sync_sector_data())


def _sync_financials_wrapper():
    from app.services.data_sync import sync_financials
    logger.info("Scheduled: sync_financials")
    _run_async(sync_financials())


def _compute_engines_wrapper():
    from app.services.stock_pool_engine import StockPoolEngine
    from app.services.sector_analysis import SectorAnalysisEngine
    from app.services.market_review import MarketReviewEngine
    from app.services.risk_scanner import RiskScanner
    logger.info("Scheduled: compute_all_engines")
    _run_async(_run_all_engines())


async def _run_all_engines():
    from app.services.stock_pool_engine import StockPoolEngine
    from app.services.sector_analysis import SectorAnalysisEngine
    from app.services.market_review import MarketReviewEngine
    from app.services.risk_scanner import RiskScanner
    await StockPoolEngine().compute_all()
    await SectorAnalysisEngine().compute_all()
    await MarketReviewEngine().compute()
    scanner = RiskScanner()
    await scanner.scan_risk_list()


def _settle_guesses_wrapper():
    from app.api.credits import settle_market_guesses
    logger.info("Scheduled: settle_market_guesses")
    _run_async(settle_market_guesses())
