"""FastAPI 入口——SQLite + 内存缓存 + APScheduler。零外部软件。

启动: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.database import init_db
from app.core.exceptions import AppError, app_error_handler, http_exception_handler
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.core.settings import get_settings
from app.models.schemas.common import APIResponse

_settings = get_settings()
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {_settings.app_name}")
    logger.info(f"DB: SQLite | Cache: In-Memory | Scheduler: APScheduler")

    if not _settings.tushare_token:
        logger.warning("TUSHARE_TOKEN 未配置 — 请在 backend/.env 中填入密钥")
    else:
        logger.info(f"Tushare token: ...{_settings.tushare_token[-8:]}")

    await init_db()

    # 种子管理员账户
    from app.core.security import seed_admin
    await seed_admin()

    start_scheduler()

    # 启动时自动同步数据并运行离线计算（后台异步，不阻塞应用启动）
    async def _auto_sync():
        try:
            from app.services.data_sync import sync_daily_data, sync_stock_basic

            logger.info("Auto-sync: stock basic...")
            await sync_stock_basic()
            logger.info("Auto-sync: daily data...")
            await sync_daily_data()

            # 首次安装时预同步历史日线（行数 < 5万 才执行，已有数据跳过）
            from app.core.database import async_session
            from sqlalchemy import text
            async with async_session() as sess:
                r = await sess.execute(text("SELECT COUNT(*) FROM stock_daily"))
                daily_count = r.scalar() or 0
            if daily_count < 50000:
                logger.info(f"Stock daily rows: {daily_count} (< 50000), starting historical sync...")
                from app.services.data_sync import sync_historical_daily
                hist_result = await sync_historical_daily(days=120)
                logger.info(f"Historical sync complete: {hist_result}")

            from app.services.stock_pool_engine import StockPoolEngine
            from app.services.sector_analysis import SectorAnalysisEngine
            from app.services.market_review import MarketReviewEngine
            from app.services.risk_scanner import RiskScanner

            logger.info("Auto-sync: stock basic...")
            await sync_stock_basic()
            logger.info("Auto-sync: daily data...")
            await sync_daily_data()
            logger.info("Auto-sync: computing engines...")
            await StockPoolEngine().compute_all()
            await SectorAnalysisEngine().compute_all()
            await MarketReviewEngine().compute()
            scanner = RiskScanner()
            await scanner.scan_risk_list()
            logger.info("Auto-sync: all engines complete")
        except Exception as e:
            logger.warning(f"Auto-sync skipped (Tushare may be unavailable): {e}")

    import asyncio
    asyncio.create_task(_auto_sync())

    yield

    shutdown_scheduler()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=_settings.app_name,
        description="A股量化分析助手 — Vue3 + FastAPI + SQLite",
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, http_exception_handler)

    # API 路由注册
    from app.api.auth import router as auth_router
    from app.api.stock_pool import router as stock_pool_router
    from app.api.diagnosis import router as diagnosis_router
    from app.api.alerts import router as alerts_router
    from app.api.market import review_router, risk_router, sector_router, market_router
    from app.api.backtest import router as backtest_router
    from app.api.admin import router as admin_router
    from app.api.membership import router as membership_router
    from app.api.user import router as user_router

    app.include_router(auth_router)
    app.include_router(stock_pool_router)
    app.include_router(diagnosis_router)
    app.include_router(alerts_router)
    app.include_router(sector_router)
    app.include_router(review_router)
    app.include_router(risk_router)
    app.include_router(market_router)
    app.include_router(backtest_router)
    app.include_router(admin_router)
    app.include_router(membership_router)
    app.include_router(user_router)

    @app.get("/api/v1/health")
    async def health():
        return APIResponse(
            code=200, message="ok",
            data={
                "app": _settings.app_name,
                "version": "2.0.0",
                "db": "SQLite",
                "cache": "In-Memory",
                "scheduler": "APScheduler",
            },
            timestamp=int(time.time()),
        ).model_dump()

    # Serve Vue3 frontend static files
    import os
    from pathlib import Path
    settings_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frontend_path = (Path(settings_dir) / ".." / "frontend").resolve()
    if frontend_path.exists():
        app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
        logger.info(f"Frontend: {frontend_path}")
    else:
        logger.warning(f"Frontend not found at {frontend_path}")

    return app


app = create_app()
