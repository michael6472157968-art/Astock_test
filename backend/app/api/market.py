"""市场行情 API——板块分析、每日复盘、风险避雷、指数行情、市场情绪。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.core.cache import cache_get
from app.middleware.auth_middleware import require_auth_optional
from app.models.schemas.common import APIResponse

logger = logging.getLogger("market")

sector_router = APIRouter(prefix="/api/v1/sector-rotation", tags=["板块轮动"])
sector_heat_router = APIRouter(prefix="/api/v1/sector", tags=["板块行情"])
review_router = APIRouter(prefix="/api/v1/review", tags=["每日复盘"])
risk_router = APIRouter(prefix="/api/v1/risk-list", tags=["风险避雷"])

market_router = APIRouter(prefix="/api/v1/market", tags=["市场概览"])

INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]


def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 540 <= t <= 930


@market_router.get("/stock_count")
async def stock_count(user: dict = Depends(require_auth_optional)):
    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    cached = await cache_get("market:stock_count")
    if cached is not None:
        return APIResponse(data={"count": cached}, timestamp=int(time.time()))

    async with async_session() as sess:
        r = await sess.execute(text("SELECT COUNT(*) FROM stocks"))
        count = r.scalar() or 0

    await cache_set("market:stock_count", count, ttl=7200)
    return APIResponse(data={"count": count}, timestamp=int(time.time()))


@market_router.get("/index")
async def market_index(user: dict = Depends(require_auth_optional)):
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    today_key = date.today().strftime("%Y%m%d")
    cache_key = f"market:index:{today_key}"
    cached = await cache_get(cache_key)
    if cached is not None:
        trade_date = today_key
        # Try to get the actual trade date for the cached response
        async with async_session() as sess:
            r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
            latest_td = r.scalar()
            if latest_td:
                trade_date = latest_td
        return APIResponse(data={"indices": cached, "source": "tushare", "update_time": now_ts, "trading": trading, "date": trade_date}, timestamp=int(time.time()))

    # 非交易时段：回退到最新交易日数据
    results = []
    trade_date = today_key
    async with async_session() as sess:
        r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
        latest_trade_date = r.scalar()
        if latest_trade_date:
            trade_date = latest_trade_date
        for code, name in INDEX_CODES:
            r = await sess.execute(
                text("SELECT close, pct_chg, change FROM stock_daily WHERE ts_code=:code AND trade_date=:td ORDER BY trade_date DESC LIMIT 1"),
                {"code": code, "td": trade_date},
            )
            row = r.first()
            if row:
                results.append({"code": code, "name": name, "close": round(row[0], 2), "pct_chg": round(row[1], 2), "change": round(row[2], 2)})

    if results and all(r["close"] for r in results):
        ttl = 300 if trading else 3600
        await cache_set(cache_key, results, ttl=ttl)
        return APIResponse(data={"indices": results, "source": "tushare", "update_time": now_ts, "trading": trading, "date": trade_date}, timestamp=int(time.time()))

    # DB 为空时调 Tushare API
    try:
        from app.services.tushare_client import call_tushare
        results = []
        for code, name in INDEX_CODES:
            df = await call_tushare("index_daily", ts_code=code, start_date=(date.today() - timedelta(days=7)).strftime("%Y%m%d"))
            if df is not None and not (hasattr(df, "empty") and df.empty):
                df_sorted = df.sort_values("trade_date", ascending=False)
                latest = df_sorted.iloc[0]
                results.append({
                    "code": code, "name": name,
                    "close": round(float(latest.get("close", 0)), 2),
                    "pct_chg": round(float(latest.get("pct_chg", 0)), 2),
                    "change": round(float(latest.get("change", 0)), 2),
                })
        if results:
            ttl = 300 if trading else 3600
            await cache_set(cache_key, results, ttl=ttl)
            return APIResponse(data={"indices": results, "source": "tushare", "update_time": now_ts, "trading": trading, "date": trade_date}, timestamp=int(time.time()))
    except Exception:
        pass

    return APIResponse(data={"indices": [], "source": "none", "update_time": now_ts, "trading": False}, timestamp=int(time.time()),
                       ext_info={"note": "暂无指数数据，请执行数据同步"})


@market_router.get("/mood")
async def market_mood(user: dict = Depends(require_auth_optional)):
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    from app.core.cache import cache_get, cache_set
    from app.core.database import async_session
    from sqlalchemy import text

    today = date.today()
    mood_data = None

    for offset in range(4):
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"market:mood:{td}")
        if cached is not None:
            cached["source"] = "tushare"
            cached["update_time"] = now_ts
            return APIResponse(data=cached, timestamp=int(time.time()))

        async with async_session() as sess:
            r = await sess.execute(
                text("SELECT COUNT(*) FILTER (WHERE pct_chg > 0) as up, "
                     "COUNT(*) FILTER (WHERE pct_chg < 0) as down, "
                     "COUNT(*) FILTER (WHERE pct_chg = 0) as flat "
                     "FROM stock_daily WHERE trade_date = :td"),
                {"td": td},
            )
            row = r.first()
            if row and (row[0] or row[1]):
                limit_up = 0
                limit_down = 0
                limit_r = await sess.execute(
                    text("SELECT COUNT(*) FILTER (WHERE pct_chg >= 9.9) as up, "
                         "COUNT(*) FILTER (WHERE pct_chg <= -9.9) as down "
                         "FROM stock_daily WHERE trade_date = :td"),
                    {"td": td},
                )
                lr = limit_r.first()
                if lr:
                    limit_up = lr[0] or 0
                    limit_down = lr[1] or 0

                mood_data = {
                    "up": int(row[0]), "down": int(row[1]), "flat": int(row[2]),
                    "limit_up": limit_up, "limit_down": limit_down,
                    "date": td, "source": "tushare", "update_time": now_ts,
                }
                break

    if not mood_data:
        return APIResponse(
            data={"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
                  "date": "", "source": "none", "update_time": now_ts},
            timestamp=int(time.time()),
            ext_info={"note": "暂无市场情绪数据，请执行数据同步"},
        )

    is_today = mood_data["date"] == today.strftime("%Y%m%d")
    ttl = 300 if is_today else 86400
    cache_data = {k: v for k, v in mood_data.items() if k not in ("source", "update_time")}
    await cache_set(f"market:mood:{mood_data['date']}", cache_data, ttl=ttl)
    return APIResponse(data=mood_data, timestamp=int(time.time()))


# ── 板块行情热力图 ──

async def _akshare_sector_heat() -> list[dict]:
    """AKShare 实时板块行情——概念板块。"""
    import akshare as ak
    df = ak.stock_board_concept_name_em()
    if df is None or df.empty:
        return []
    sectors = []
    for _, row in df.head(50).iterrows():
        sectors.append({
            "name": str(row.get("板块名称", "")),
            "code": str(row.get("板块代码", "")),
            "pct_chg": round(float(row.get("涨跌幅", 0)), 2),
            "leading_stock": str(row.get("领涨股票", "")),
        })
    return sectors


async def _tushare_concept_heat(trade_date: str) -> list[dict]:
    """DB 行业聚合——按 industry 分组计算板块涨跌幅（concept_daily 需要更高点数）。"""
    from app.core.database import async_session
    from sqlalchemy import text
    async with async_session() as sess:
        r = await sess.execute(text("""
            SELECT s.industry,
                   ROUND(AVG(d.pct_chg), 2) as avg_pct,
                   COUNT(*) as cnt
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td AND s.industry != ''
            GROUP BY s.industry
            HAVING COUNT(*) >= 5
            ORDER BY avg_pct DESC
            LIMIT 50
        """), {"td": trade_date})
        sectors = []
        for row in r:
            sectors.append({
                "name": row[0],
                "code": "",
                "pct_chg": round(float(row[1]), 2),
                "leading_stock": "",
            })
        return sectors


async def _db_sector_heat(trade_date: str) -> list[dict]:
    """从 DB 读取 sector_analysis 缓存。"""
    cached = await cache_get(f"sector:ranking:{trade_date}")
    if not cached:
        return []
    sectors = []
    for s in cached[:50]:
        sectors.append({
            "name": s.get("name", ""),
            "code": "",
            "pct_chg": s.get("avg_pct", 0),
            "leading_stock": "",
        })
    return sectors


@sector_heat_router.get("/heat")
async def sector_heat(limit: int = 0, user: dict = Depends(require_auth_optional)):
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    cache_ttl = 600 if trading else 86400
    cache_key = "sector:heat:v1"
    cached = await cache_get(cache_key)
    if cached is not None:
        if limit and limit > 0 and len(cached.get("sectors", [])) > limit:
            result = dict(cached)
            result["total"] = len(result["sectors"])
            result["sectors"] = sorted(result["sectors"], key=lambda s: abs(s["pct_chg"]), reverse=True)[:limit]
            result["has_more"] = True
            return APIResponse(data=result, timestamp=int(time.time()))
        return APIResponse(data=cached, timestamp=int(time.time()))

    sectors: list[dict] = []
    source = "none"

    # 1. Try AKShare
    try:
        sectors = await asyncio.wait_for(_akshare_sector_heat(), timeout=15)
        if sectors:
            source = "akshare_realtime"
            logger.info(f"Sector heat from AKShare: {len(sectors)} sectors")
    except Exception as e:
        logger.warning(f"AKShare sector heat failed: {e}")

    # 2. Fallback to DB industry aggregation
    if not sectors:
        for offset in range(4):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                sectors = await _tushare_concept_heat(td)
                if sectors:
                    source = "db_industry"
                    logger.info(f"Sector heat from DB ({td}): {len(sectors)} sectors")
                    break
            except Exception as e:
                logger.warning(f"DB sector heat ({td}) failed: {e}")

    # 3. Fallback to DB cache
    if not sectors:
        for offset in range(4):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            sectors = await _db_sector_heat(td)
            if sectors:
                source = "db_cache"
                logger.info(f"Sector heat from DB cache ({td}): {len(sectors)} sectors")
                break

    data = {
        "sectors": sectors,
        "source": source,
        "date": date.today().strftime("%Y%m%d"),
    }

    if limit and limit > 0 and len(sectors) > limit:
        data["total"] = len(sectors)
        data["sectors"] = sorted(sectors, key=lambda s: abs(s["pct_chg"]), reverse=True)[:limit]
        data["has_more"] = True

    from app.core.cache import cache_set
    await cache_set(cache_key, data, ttl=cache_ttl)
    return APIResponse(data=data, timestamp=int(time.time()))


@sector_heat_router.get("/{sector_code}/leaders")
async def sector_leaders(sector_code: str, user: dict = Depends(require_auth_optional)):
    """获取某个板块领涨股 Top 5"""
    cached = await cache_get(f"sector:leaders:{sector_code}")
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

    from app.core.cache import cache_set

    stocks: list[dict] = []
    sector_name = sector_code

    # 1. Try AKShare
    try:
        import akshare as ak
        df = await asyncio.wait_for(
            asyncio.to_thread(ak.stock_board_concept_cons_em, symbol=sector_code),
            timeout=15,
        )
        if df is not None and not df.empty:
            sector_name = str(df.iloc[0].get("板块名称", sector_code)) if "板块名称" in df.columns else sector_code
            df_sorted = df.sort_values("涨跌幅", ascending=False) if "涨跌幅" in df.columns else df
            for _, row in df_sorted.head(5).iterrows():
                raw_code = str(row.get("代码", ""))
                pct = round(float(row.get("涨跌幅", 0)), 2)
                close = round(float(row.get("最新价", 0)), 2)
                name = str(row.get("名称", ""))
                ts_code = _normalize_ts_code(raw_code)
                stocks.append({"ts_code": ts_code, "name": name, "close": close, "pct_chg": pct})
            logger.info(f"Sector leaders from AKShare for {sector_code}: {len(stocks)}")
    except Exception as e:
        logger.warning(f"AKShare sector leaders failed for {sector_code}: {e}")

    # 2. Fallback to Tushare concept_detail
    if not stocks:
        try:
            from app.services.tushare_client import call_tushare
            df = await call_tushare("concept_detail", code=sector_code)
            if df is not None and not (hasattr(df, "empty") and df.empty):
                if "name" in df.columns:
                    sector_name = str(df.iloc[0].get("name", sector_code))
                df_sorted = df.sort_values("pct_chg", ascending=False) if "pct_chg" in df.columns else df
                for _, row in df_sorted.head(5).iterrows():
                    raw_code = str(row.get("ts_code", ""))
                    pct = round(float(row.get("pct_chg", 0)), 2)
                    close = round(float(row.get("close", 0)), 2)
                    name = str(row.get("name", ""))
                    stocks.append({"ts_code": raw_code, "name": name, "close": close, "pct_chg": pct})
                logger.info(f"Sector leaders from Tushare for {sector_code}: {len(stocks)}")
        except Exception as e:
            logger.warning(f"Tushare concept_detail failed for {sector_code}: {e}")

    # 3. Fallback to DB
    if not stocks:
        from app.core.database import async_session
        from sqlalchemy import text
        for offset in range(4):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            async with async_session() as sess:
                r = await sess.execute(text("""
                    SELECT s.industry FROM stocks s WHERE s.ts_code LIKE :prefix LIMIT 1
                """), {"prefix": f"%{sector_code}%"})
                row = r.first()
                if row:
                    sector_name = row[0]
                r = await sess.execute(text("""
                    SELECT d.ts_code, s.name, d.close, d.pct_chg
                    FROM stock_daily d
                    JOIN stocks s ON s.ts_code = d.ts_code
                    WHERE d.trade_date = :td AND s.industry = :ind
                    ORDER BY d.pct_chg DESC
                    LIMIT 5
                """), {"td": td, "ind": sector_name})
                for row in r:
                    stocks.append({
                        "ts_code": row[0], "name": row[1],
                        "close": round(float(row[2] if row[2] else 0), 2),
                        "pct_chg": round(float(row[3] if row[3] else 0), 2),
                    })
                if stocks:
                    break

    data = {"sector_name": sector_name, "stocks": stocks}
    ttl = 600 if _is_trading_time() else 86400
    await cache_set(f"sector:leaders:{sector_code}", data, ttl=ttl)
    return APIResponse(data=data, timestamp=int(time.time()))


def _normalize_ts_code(raw: str) -> str:
    """AKShare 返回的纯数字代码转 Tushare 格式。"""
    code = raw.strip()
    if "." in code:
        return code
    if code.startswith("60") or code.startswith("68"):
        return code + ".SH"
    if code.startswith("00") or code.startswith("30"):
        return code + ".SZ"
    return code


# ── 旧板块分析（保留兼容）──

@sector_router.get("/ranking")
async def sector_ranking(user: dict = Depends(require_auth_optional)):
    today = date.today()
    for offset in [1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"sector:ranking:{td}")
        if cached:
            return APIResponse(data={"date": td, "sectors": cached}, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "sectors": []}, timestamp=int(time.time()),
                       ext_info={"note": "请先在管理后台执行数据同步和板块分析计算"})


@review_router.get("/latest")
async def latest_review(user: dict = Depends(require_auth_optional)):
    from app.core.cache import cache_get
    from datetime import date, timedelta
    today = date.today()
    for offset in [0, 1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"review:{td}")
        if cached:
            return APIResponse(data=cached, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "content": {}}, timestamp=int(time.time()),
                       ext_info={"note": "需要先运行数据同步"})


@review_router.get("/daily")
async def review_daily(date: str = "", user: dict = Depends(require_auth_optional)):
    from app.core.cache import cache_get, cache_set
    from datetime import date as _date, timedelta
    from app.services.market_review import MarketReviewEngine

    if not date:
        today = _date.today()
        date = (today - timedelta(days=1)).strftime("%Y%m%d")

    cached = await cache_get(f"review:{date}")
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    engine = MarketReviewEngine()
    review_data = await engine.compute(date)
    if review_data:
        await cache_set(f"review:{date}", review_data, ttl=86400 * 60)

    return APIResponse(data=review_data, timestamp=int(time.time()))


@risk_router.get("")
async def risk_list(page: int = 1, page_size: int = 20, user: dict = Depends(require_auth_optional)):
    from app.core.cache import cache_get
    from app.core.database import async_session
    from app.models.orm.models import RiskListResult
    from sqlalchemy import select, func
    from datetime import date, timedelta

    today = date.today()
    items = []
    for offset in [0, 1, 2, 3]:
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"risk:list:{td}")
        if cached:
            items = cached
            break

    if not items:
        from app.core.cache import cache_set
        async with async_session() as session:
            r = await session.execute(
                select(func.max(RiskListResult.calc_date))
            )
            latest_date = r.scalar_one_or_none()
            if latest_date:
                r = await session.execute(
                    select(RiskListResult).where(RiskListResult.calc_date == latest_date)
                )
                rows = r.scalars().all()
                items = [
                    {"risk_category": row.risk_category, "ts_code": row.ts_code,
                     "stock_name": row.stock_name, "risk_detail": row.risk_detail}
                    for row in rows
                ]
                if items:
                    await cache_set(f"risk:list:{latest_date}", items, ttl=86400)

    if not items:
        return APIResponse(
            data={"total": 0, "page": page, "page_size": page_size, "items": []},
            timestamp=int(time.time()),
            ext_info={"note": "暂无风险扫描数据，请在管理后台执行数据同步后运行风险扫描"},
        )

    total = len(items)
    start = (page - 1) * page_size
    paged = items[start:start + page_size]

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": paged},
        timestamp=int(time.time()),
    )
