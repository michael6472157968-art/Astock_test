"""FastAPI 入口——SQLite + 内存缓存 + APScheduler。零外部软件。

启动: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

    from app.utils.trading_calendar import ensure_calendar_table
    await ensure_calendar_table()

    start_scheduler()

    # 启动时轻量同步：股票列表 + 日线 + 每日指标。计算引擎随后触发。
    async def _auto_sync():
        import asyncio as _asyncio
        await _asyncio.sleep(5)  # 给首批请求让路，避免 SQLite 读写争锁
        try:
            from app.services.data_sync import sync_stock_basic, sync_daily_data, sync_daily_basic, sync_moneyflow_hsgt

            logger.info("Auto-sync: stock basic...")
            await sync_stock_basic()
            logger.info("Auto-sync: daily data...")
            await sync_daily_data()
            logger.info("Auto-sync: daily basic...")
            await sync_daily_basic()
            logger.info("Auto-sync: moneyflow hsgt...")
            await sync_moneyflow_hsgt()

            from app.core.database import async_session
            from sqlalchemy import text
            async with async_session() as sess:
                r = await sess.execute(text("SELECT COUNT(*) FROM stock_daily"))
                daily_count = r.scalar() or 0
            if daily_count < 50000:
                logger.info(f"Stock daily rows: {daily_count} (< 50000), starting historical sync...")
                from app.services.data_sync import sync_historical_daily, sync_index_historical
                hist_result = await sync_historical_daily(days=120)
                logger.info(f"Historical sync complete: {hist_result}")
                # 回填指数日线，否则日历只有最新交易日有涨跌染色
                await sync_index_historical(days=120)
            else:
                logger.info(f"Stock daily rows: {daily_count} (>= 50000), skip historical sync")

            # 数据同步完成后触发计算引擎，确保启动后数据都是新的
            logger.info("Auto-sync: compute engines...")
            from app.services.stock_pool_engine import StockPoolEngine
            from app.services.short_term_engine import ShortTermEngine
            from app.services.sector_analysis import SectorAnalysisEngine
            from app.services.market_review import MarketReviewEngine
            from app.services.risk_scanner import RiskScanner
            from app.core.cache import cache_clear
            await StockPoolEngine().compute_all()
            await ShortTermEngine().compute_all()
            await SectorAnalysisEngine().compute_all()
            await MarketReviewEngine().compute()
            scanner = RiskScanner()
            await scanner.scan_risk_list()
            # 清除所有内存缓存，让首页API重新从DB读取最新数据
            await cache_clear()
            logger.info("Auto-sync: all engines done")
        except Exception as e:
            logger.warning(f"Auto-sync failed: {e}")

    # 只在空库首次部署时跑全量同步，避免每次重启拖慢 API 响应
    async def _maybe_auto_sync():
        await asyncio.sleep(5)
        from app.core.database import async_session
        from sqlalchemy import text
        async with async_session() as s:
            r = await s.execute(text("SELECT COUNT(*) FROM stocks"))
            count = r.scalar()
            if count and count > 0:
                logger.info("Auto-sync: data exists, skip")
                return
        await _auto_sync()

    asyncio.create_task(_maybe_auto_sync())

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

    from fastapi.exceptions import HTTPException as FastAPIHTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException

    async def _http_exception_handler(request: Request, exc: Exception):
        status_code = exc.status_code if hasattr(exc, 'status_code') else 500
        detail = exc.detail if hasattr(exc, 'detail') else str(exc)
        return JSONResponse(
            status_code=status_code,
            content={
                "code": status_code,
                "message": detail,
                "data": None,
                "timestamp": int(time.time()),
                "ext_info": {},
            },
        )

    app.add_exception_handler(FastAPIHTTPException, _http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)

    # API 路由注册
    from app.api.auth import router as auth_router
    from app.api.stock_pool import router as stock_pool_router
    from app.api.diagnosis import router as diagnosis_router
    from app.api.alerts import router as alerts_router
    from app.api.market import review_router, risk_router, sector_router, sector_heat_router, market_router
    from app.api.backtest import router as backtest_router
    from app.api.admin import router as admin_router
    from app.api.membership import router as membership_router
    from app.api.credits import router as credits_router
    from app.api.user import router as user_router

    app.include_router(auth_router)
    app.include_router(stock_pool_router)
    app.include_router(diagnosis_router)
    app.include_router(alerts_router)
    app.include_router(sector_router)
    app.include_router(sector_heat_router)
    app.include_router(review_router)
    app.include_router(risk_router)
    app.include_router(market_router)
    app.include_router(backtest_router)
    app.include_router(admin_router)
    app.include_router(membership_router)
    app.include_router(credits_router)
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
        # CSS/JS/lib 带 cache 头（文件名含版本号 v=8，更新时改版本号即跳过缓存）
        for sub_dir in ["/css", "/js", "/lib"]:
            sp = frontend_path / sub_dir.lstrip("/")
            if sp.is_dir():
                app.mount(sub_dir, StaticFiles(directory=str(sp)), name=f"static-{sub_dir.lstrip('/')}")
        # HTML 根 mount 兜底，不加缓存
        app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")

        # 给静态资源响应追加 Cache-Control 头（Starlette StaticFiles 不支持 headers 参数）
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        class _CacheStaticMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                path = request.url.path
                if path.startswith("/css/") or path.startswith("/js/") or path.startswith("/lib/"):
                    response.headers["Cache-Control"] = "public, max-age=31536000"
                return response
        app.add_middleware(_CacheStaticMiddleware)
        logger.info(f"Frontend: {frontend_path}")
    else:
        logger.warning(f"Frontend not found at {frontend_path}")

    return app


app = create_app()
