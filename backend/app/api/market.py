"""市场行情 API——板块分析、每日复盘、风险避雷、指数行情、市场情绪。"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.core.cache import cache_get, cache_set, cache_delete
from app.middleware.auth_middleware import require_auth_optional
from app.models.schemas.common import APIResponse
from app.utils.trading_calendar import get_latest_trade_date, get_next_trade_date, get_trade_days_in_range, is_trade_date

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


async def _get_trade_context() -> tuple[str, bool]:
    """返回 (trade_date, is_trade_day) — 统一入口，所有端点共用。

    优先级：stock_daily MAX(trade_date) → get_latest_trade_date() → date.today()
    """
    from app.core.database import async_session
    from sqlalchemy import text

    today_str = date.today().strftime("%Y%m%d")
    trade_date = today_str

    # 1. DB MAX 优先——stock_daily 里的最新日期就是数据截止日
    try:
        async with async_session() as sess:
            r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
            max_td = r.scalar()
            if max_td:
                trade_date = max_td
    except Exception:
        pass

    # 2. DB 无数据时回退到交易日历
    if trade_date == today_str:
        try:
            trade_date = await get_latest_trade_date()
        except Exception:
            pass

    try:
        is_td_val = await is_trade_date(today_str)
    except Exception:
        is_td_val = date.today().weekday() < 5

    return trade_date, is_td_val


@market_router.get("/stock_count")
async def stock_count(user: dict = Depends(require_auth_optional)):
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

    trade_date, is_td = await _get_trade_context()

    from app.core.database import async_session
    from sqlalchemy import text

    cache_key = f"market:index:{trade_date}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return APIResponse(data={
            "indices": cached, "source": "tushare", "update_time": now_ts,
            "trading": trading, "date": trade_date, "trade_date": trade_date,
            "is_trade_day": is_td,
        }, timestamp=int(time.time()))

    results = []
    async with async_session() as sess:
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
        return APIResponse(data={
            "indices": results, "source": "tushare", "update_time": now_ts,
            "trading": trading, "date": trade_date, "trade_date": trade_date,
            "is_trade_day": is_td,
        }, timestamp=int(time.time()))

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
            return APIResponse(data={
                "indices": results, "source": "tushare", "update_time": now_ts,
                "trading": trading, "date": trade_date, "trade_date": trade_date,
                "is_trade_day": is_td,
            }, timestamp=int(time.time()))
    except Exception:
        pass

    return APIResponse(data={
        "indices": [], "source": "none", "update_time": now_ts,
        "trading": False, "date": trade_date, "trade_date": trade_date,
        "is_trade_day": is_td,
    }, timestamp=int(time.time()), ext_info={"note": "暂无指数数据，请执行数据同步"})


@market_router.get("/dashboard")
async def market_dashboard(user: dict = Depends(require_auth_optional)):
    """首页仪表盘聚合数据——北向资金、成交额、行业宽度、板块热力预览。"""
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()
    trade_date, is_td = await _get_trade_context()

    ck = f"market:dashboard:v2:{trade_date}"
    cached = await cache_get(ck)
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

    from app.core.database import async_session

    result: dict = {
        "trade_date": trade_date,
        "is_trade_day": is_td,
        "update_time": now_ts,
    }

    async with async_session() as sess:
        # 1. 全市场成交额 & 行业宽度
        r = await sess.execute(text("""
            SELECT SUM(d.amount) as turnover,
                   SUM(d.volume) as volume,
                   COUNT(*) as cnt,
                   COUNT(*) FILTER (WHERE d.pct_chg > 0) as up_cnt,
                   COUNT(*) FILTER (WHERE d.pct_chg < 0) as down_cnt
            FROM stock_daily d
            WHERE d.trade_date = :td
        """), {"td": trade_date})
        row = r.first()
        if row and row[0]:
            total = row[2] or 1
            result["turnover"] = round(float(row[0]), 2)
            result["volume"] = round(float(row[1]), 2)
            result["stock_count"] = total
            result["up_stocks"] = row[3] or 0
            result["down_stocks"] = row[4] or 0
            result["breath"] = round((row[3] or 0) / total * 100, 1)

        # 2. 行业宽度——行业维度涨跌
        r2 = await sess.execute(text("""
            SELECT s.industry,
                   ROUND(AVG(d.pct_chg), 2) as avg_pct,
                   COUNT(*) as cnt
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td AND s.industry != ''
            GROUP BY s.industry
            HAVING COUNT(*) >= 5
        """), {"td": trade_date})
        industries = [(row2[0], float(row2[1]), row2[2]) for row2 in r2]
        if industries:
            result["industry_up"] = sum(1 for _, pct, _ in industries if pct > 0)
            result["industry_down"] = sum(1 for _, pct, _ in industries if pct < 0)
            result["industry_flat"] = len(industries) - result["industry_up"] - result["industry_down"]
            result["industry_breadth"] = round(result["industry_up"] / len(industries) * 100, 1)
            # Top 8 by abs(pct_chg) for mini heatmap
            top8 = sorted(industries, key=lambda x: abs(x[1]), reverse=True)[:8]
            result["top_sectors"] = [{"name": n, "pct_chg": pct, "cnt": c} for n, pct, c in top8]

    # 3. 北向资金
    try:
        from app.services.tushare_client import get_moneyflow_hsgt
        end_d = trade_date
        start_d = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
        nf = await get_moneyflow_hsgt(start_d, end_d)
        if nf:
            nf_sorted = sorted(nf, key=lambda x: x.get("trade_date", ""), reverse=True)
            latest = nf_sorted[0]
            north_val = float(latest.get("north_money", 0) or 0) * 1e4  # Tushare万元→元
            result["northbound"] = {
                "date": trade_date,
                "net_in": round(north_val, 2),
                "ggt_ss": round(float(latest.get("ggt_ss", 0) or 0), 2),
                "ggt_sz": round(float(latest.get("ggt_sz", 0) or 0), 2),
                "hgt": round(float(latest.get("hgt", 0) or 0), 2),
                "sgt": round(float(latest.get("sgt", 0) or 0), 2),
            }
            # 近期5日趋势
            result["northbound"]["recent"] = [
                {"date": x.get("trade_date", ""), "net_in": round(float(x.get("north_money", 0) or 0) * 1e4, 2)}
                for x in nf_sorted[:5]
            ][::-1]  # oldest first
    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")
        result["northbound"] = None

    ttl = 300 if trading else 3600
    await cache_set(ck, result, ttl=ttl)
    return APIResponse(data=result, timestamp=int(time.time()))


@market_router.get("/mood")
async def market_mood(user: dict = Depends(require_auth_optional)):
    now_ts = datetime.now().isoformat()
    trading = _is_trading_time()

    trade_date, is_td = await _get_trade_context()

    from app.core.database import async_session
    from sqlalchemy import text

    mood_data = None

    for offset in range(4):
        td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"market:mood:{td}")
        if cached is not None:
            cached["source"] = "tushare"
            cached["update_time"] = now_ts
            cached["is_trade_day"] = is_td
            cached["trade_date"] = trade_date
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
                    text("SELECT COUNT(*) FILTER (WHERE limit_type != '跌停池') as up, "
                         "COUNT(*) FILTER (WHERE limit_type = '跌停池') as down "
                         "FROM limit_list_records WHERE trade_date = :td"),
                    {"td": td},
                )
                lr = limit_r.first()
                if lr and (lr[0] or lr[1]):
                    limit_up = lr[0] or 0
                    limit_down = lr[1] or 0
                else:
                    # 降级：limit_list_records 为空时用 pct_chg 估算
                    fb_r = await sess.execute(
                        text("SELECT COUNT(*) FILTER (WHERE pct_chg >= 9.8), "
                             "COUNT(*) FILTER (WHERE pct_chg <= -9.8) "
                             "FROM stock_daily WHERE trade_date = :td"),
                        {"td": td},
                    )
                    fb = fb_r.first()
                    if fb:
                        limit_up = fb[0] or 0
                        limit_down = fb[1] or 0

                mood_data = {
                    "up": int(row[0]), "down": int(row[1]), "flat": int(row[2]),
                    "limit_up": limit_up, "limit_down": limit_down,
                    "date": td, "source": "tushare", "update_time": now_ts,
                    "is_trade_day": is_td, "trade_date": trade_date,
                }
                break

    if not mood_data:
        return APIResponse(
            data={"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
                  "date": "", "source": "none", "update_time": now_ts,
                  "is_trade_day": is_td, "trade_date": trade_date},
            timestamp=int(time.time()),
            ext_info={"note": "暂无市场情绪数据，请执行数据同步"})

    is_today = mood_data["date"] == date.today().strftime("%Y%m%d")
    ttl = 300 if is_today else 86400
    cache_data = {k: v for k, v in mood_data.items() if k not in ("source", "update_time", "is_trade_day", "trade_date")}
    await cache_set(f"market:mood:{mood_data['date']}", cache_data, ttl=ttl)
    return APIResponse(data=mood_data, timestamp=int(time.time()))


# ── 板块行情热力图 ──

async def _tushare_concept_heat(trade_date: str) -> list[dict]:
    """DB 行业聚合——按 industry 分组计算板块涨跌幅。"""
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
    trading = _is_trading_time()
    trade_date, is_td = await _get_trade_context()
    cache_ttl = 600 if trading else 86400
    cache_key = f"sector:heat:v3:{trade_date}"
    cached = await cache_get(cache_key)
    if cached is not None:
        result = dict(cached)
        result["is_trade_day"] = is_td
        result["trade_date"] = trade_date
        if limit and limit > 0 and len(result.get("sectors", [])) > limit:
            result["total"] = len(result["sectors"])
            result["sectors"] = sorted(result["sectors"], key=lambda s: abs(s["pct_chg"]), reverse=True)[:limit]
            result["has_more"] = True
        return APIResponse(data=result, timestamp=int(time.time()))

    sectors: list[dict] = []

    # 1. DB 行业聚合
    for offset in range(4):
        td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            sectors = await _tushare_concept_heat(td)
            if sectors:
                logger.info(f"Sector heat from DB ({td}): {len(sectors)} sectors")
                break
        except Exception as e:
            logger.warning(f"DB sector heat ({td}) failed: {e}")

    # 2. Fallback to DB cache
    if not sectors:
        for offset in range(4):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            sectors = await _db_sector_heat(td)
            if sectors:
                logger.info(f"Sector heat from DB cache ({td}): {len(sectors)} sectors")
                break

    data = {
        "sectors": sectors,
        "date": trade_date,
        "trade_date": trade_date,
        "is_trade_day": is_td,
    }
    if not is_td:
        data["message"] = f"今日非交易日，显示 {trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} 数据"

    if limit and limit > 0 and len(sectors) > limit:
        data["total"] = len(sectors)
        data["sectors"] = sorted(sectors, key=lambda s: abs(s["pct_chg"]), reverse=True)[:limit]
        data["has_more"] = True

    await cache_set(cache_key, data, ttl=cache_ttl)
    return APIResponse(data=data, timestamp=int(time.time()))


@sector_heat_router.get("/{sector_code}/leaders")
async def sector_leaders(sector_code: str, user: dict = Depends(require_auth_optional)):
    cached = await cache_get(f"sector:leaders:{sector_code}")
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

    trade_date, is_td = await _get_trade_context()

    stocks: list[dict] = []
    sector_name = sector_code

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

    data = {"sector_name": sector_name, "stocks": stocks, "is_trade_day": is_td, "trade_date": trade_date}
    ttl = 600 if _is_trading_time() else 86400
    await cache_set(f"sector:leaders:{sector_code}", data, ttl=ttl)
    return APIResponse(data=data, timestamp=int(time.time()))


# ── 旧板块分析（保留兼容）──

async def _load_sectors_from_db(trade_date: str) -> list:
    """从 sector_analysis_results 表读取板块分析结果。"""
    rows_list = []
    from app.core.database import async_session
    async with async_session() as sess:
        r = await sess.execute(text(
            "SELECT sector_code, heat_score, differentiation_index "
            "FROM sector_analysis_results WHERE calc_date = :cd "
            "ORDER BY heat_score DESC"
        ), {"cd": trade_date})
        for row in r:
            rows_list.append({
                "name": row[0],
                "heat_score": round(float(row[1]), 2) if row[1] else 0,
                "momentum": round(float(row[2]), 2) if row[2] else 0,
                "avg_pct": 0,
                "count": 0,
                "max_pct": 0,
                "min_pct": 0,
                "prev_5d_avg": 0,
                "phase": "",
            })
    return rows_list


@sector_router.get("/ranking")
async def sector_ranking(user: dict = Depends(require_auth_optional)):
    trade_date, _ = await _get_trade_context()
    cached = await cache_get(f"sector:ranking:{trade_date}")
    if cached:
        return APIResponse(data={"date": trade_date, "sectors": cached}, timestamp=int(time.time()))

    # 缓存 miss → 从 DB 降级读取
    sectors = await _load_sectors_from_db(trade_date)
    if sectors:
        await _cache_set(f"sector:ranking:{trade_date}", sectors, ttl=_settings.cache_offline_ttl)
        return APIResponse(data={"date": trade_date, "sectors": sectors}, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "sectors": []}, timestamp=int(time.time()),
                       ext_info={"note": "请先在管理后台执行数据同步和板块分析计算"})


REVIEW_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS review_reports (
    data_date TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 0
)
"""


async def _ensure_review_table():
    from app.core.database import async_session
    async with async_session() as sess:
        await sess.execute(text(REVIEW_TABLE_DDL))
        await sess.commit()


async def _get_review_meta(data_date: str) -> dict | None:
    """Get review metadata row from DB. Returns None if not found."""
    from app.core.database import async_session
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT data_date, generated_at, is_latest FROM review_reports WHERE data_date = :d"),
            {"d": data_date},
        )
        row = r.first()
        if row:
            return {"data_date": row[0], "generated_at": row[1], "is_latest": row[2]}
        return None


async def _save_review_meta(data_date: str, generated_at: str, is_latest: int):
    from app.core.database import async_session
    async with async_session() as sess:
        await sess.execute(
            text("INSERT OR REPLACE INTO review_reports (data_date, content, generated_at, is_latest) VALUES (:d, '{}', :g, :l)"),
            {"d": data_date, "g": generated_at, "l": is_latest},
        )
        await sess.commit()


async def _delete_review_meta(data_date: str):
    from app.core.database import async_session
    async with async_session() as sess:
        await sess.execute(text("DELETE FROM review_reports WHERE data_date = :d"), {"d": data_date})
        await sess.commit()


async def _purge_expired_reviews():
    """Delete review rows older than 7 days that are not latest."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    from app.core.database import async_session
    async with async_session() as sess:
        await sess.execute(
            text("DELETE FROM review_reports WHERE is_latest = 0 AND generated_at < :c"),
            {"c": cutoff},
        )
        await sess.commit()


async def _set_latest_flag(data_date: str):
    """Mark this date as latest, demote all others."""
    from app.core.database import async_session
    async with async_session() as sess:
        await sess.execute(text("UPDATE review_reports SET is_latest = 0"))
        await sess.execute(
            text("UPDATE review_reports SET is_latest = 1 WHERE data_date = :d"),
            {"d": data_date},
        )
        await sess.commit()


@review_router.get("/latest")
async def latest_review(user: dict = Depends(require_auth_optional)):
    trade_date, is_td = await _get_trade_context()
    cached = await cache_get(f"review:{trade_date}")
    if cached:
        cached["is_trade_day"] = is_td
        cached["trade_date"] = trade_date
        return APIResponse(data=cached, timestamp=int(time.time()))
    return APIResponse(data={"date": "", "content": {}, "is_trade_day": is_td, "trade_date": trade_date}, timestamp=int(time.time()),
                       ext_info={"note": "需要先运行数据同步"})


@review_router.get("/dates")
async def review_dates(user: dict = Depends(require_auth_optional)):
    """Return dates that have trade data in stock_daily (descending, last 60 days)."""
    from app.core.database import async_session

    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 60")
        )
        dates = [row[0] for row in r]
    return APIResponse(data={"dates": dates}, timestamp=int(time.time()))


@review_router.get("/status/{date}")
async def review_status(date: str, user: dict = Depends(require_auth_optional)):
    """Check if a review report exists and is still fresh."""
    await _ensure_review_table()
    await _purge_expired_reviews()
    meta = await _get_review_meta(date)
    if not meta:
        return APIResponse(data={"status": "not_generated", "date": date}, timestamp=int(time.time()))
    return APIResponse(data={"status": "ready", "date": date, "generated_at": meta["generated_at"], "is_latest": bool(meta["is_latest"])}, timestamp=int(time.time()))


@review_router.get("/daily")
async def review_daily(date: str = "", user: dict = Depends(require_auth_optional)):
    from app.services.market_review import MarketReviewEngine

    await _ensure_review_table()
    await _purge_expired_reviews()

    if not date:
        trade_date, is_td = await _get_trade_context()
        date = trade_date
    else:
        is_td = await is_trade_date(date)

    # Check DB meta first
    meta = await _get_review_meta(date)
    if not meta:
        # Check cache for legacy data
        cached = await cache_get(f"review:{date}")
        if cached:
            cached["status"] = "ready"
            cached["is_trade_day"] = is_td
            cached["trade_date"] = date
            cached["generated_at"] = ""
            return APIResponse(data=cached, timestamp=int(time.time())
        )
        return APIResponse(
            data={"status": "not_generated", "date": date, "content": {}, "is_trade_day": is_td, "trade_date": date},
            timestamp=int(time.time()),
            ext_info={"note": "报告未生成，请先生成"}
        )

    # Report exists in DB — load content from cache (fast path) or recompute
    cached = await cache_get(f"review:{date}")
    if cached:
        cached["status"] = "ready"
        cached["is_trade_day"] = is_td
        cached["trade_date"] = date
        cached["generated_at"] = meta["generated_at"]
        cached["is_latest"] = bool(meta["is_latest"])
        return APIResponse(data=cached, timestamp=int(time.time()))

    # Recompute content
    engine = MarketReviewEngine()
    review_data = await engine.compute(date)
    if review_data and review_data.get("content", {}).get("total"):
        review_data["status"] = "ready"
        review_data["is_trade_day"] = is_td
        review_data["trade_date"] = date
        review_data["generated_at"] = meta["generated_at"]
        review_data["is_latest"] = bool(meta["is_latest"])
        await cache_set(f"review:{date}", review_data, ttl=86400 * 60)
        return APIResponse(data=review_data, timestamp=int(time.time()))

    return APIResponse(
        data={"status": "not_generated", "date": date, "content": {}, "is_trade_day": is_td, "trade_date": date},
        timestamp=int(time.time()),
        ext_info={"note": "报告数据为空，请执行数据同步"}
    )


@review_router.post("/generate")
async def review_generate(request: Request, user: dict = Depends(require_auth_optional)):
    from app.services.market_review import MarketReviewEngine

    await _ensure_review_table()
    await _purge_expired_reviews()

    # Parse date from request body, fall back to latest trade date
    trade_date = None
    try:
        body = await request.json()
        trade_date = body.get("date", "") if body else ""
    except Exception:
        pass
    if not trade_date:
        trade_date, _ = await _get_trade_context()
    else:
        trade_date = str(trade_date).strip()

    is_td = await is_trade_date(trade_date) if trade_date else False

    engine = MarketReviewEngine()
    review_data = await engine.compute(trade_date)

    if not review_data or not review_data.get("content", {}).get("total"):
        return APIResponse(
            data={"status": "empty", "date": trade_date, "message": "该日期暂无交易数据"},
            timestamp=int(time.time()),
            ext_info={"note": "数据为空"}
        )

    # Mark lifecycle
    generated_at = datetime.now(timezone.utc).isoformat()
    is_latest_flag = 1 if trade_date == (await get_latest_trade_date()) else 0
    if is_latest_flag:
        await _set_latest_flag(trade_date)
    await _save_review_meta(trade_date, generated_at, is_latest_flag)

    # Cache it
    await cache_delete(f"review:{trade_date}")
    review_data["status"] = "ready"
    review_data["generated_at"] = generated_at
    review_data["is_latest"] = bool(is_latest_flag)
    review_data["is_trade_day"] = is_td
    review_data["trade_date"] = trade_date
    await cache_set(f"review:{trade_date}", review_data, ttl=86400 * 60)

    return APIResponse(data=review_data, timestamp=int(time.time()))


@review_router.get("/download", response_class=PlainTextResponse)
async def review_download(date: str = "", fmt: str = "md", user: dict = Depends(require_auth_optional)):
    await _ensure_review_table()

    if not date:
        trade_date, _ = await _get_trade_context()
        date = trade_date

    # Load from cache first
    cached = await cache_get(f"review:{date}")
    if not cached:
        from app.services.market_review import MarketReviewEngine
        engine = MarketReviewEngine()
        cached = await engine.compute(date)
        if cached and cached.get("content", {}).get("total"):
            await cache_set(f"review:{date}", cached, ttl=86400 * 60)

    if not cached or not cached.get("content", {}).get("total"):
        raise HTTPException(status_code=404, detail="报告不存在或数据为空")

    if fmt == "pdf":
        return _build_pdf_response(date, cached)

    c = cached["content"]
    dt_label = date[:4] + "-" + date[4:6] + "-" + date[6:8]

    md = _build_markdown(date, dt_label, c)

    from urllib.parse import quote
    filename = f"复盘报告_{date}.md"
    content_encoded = md.encode("utf-8")
    return PlainTextResponse(
        content=content_encoded,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _build_markdown(date: str, dt_label: str, c: dict) -> str:
    md = f"# 每日市场复盘简报 — {dt_label}\n\n"
    md += f"## 大盘概览\n\n"
    md += f"- 全市场: **{c['total']}** 只\n"
    md += f"- 上涨: **{c['up_count']}** | 平盘: **{c.get('flat_count', 0)}** | 下跌: **{c['down_count']}**\n"
    md += f"- 涨停: **{c['limit_up']}** 只 | 跌停: **{c['limit_down']}** 只\n"
    md += f"- 平均涨跌幅: **{c['avg_pct']:+.2f}%**\n"
    md += f"- 涨跌比: **{c.get('up_ratio', 0)}%**\n\n"

    if c.get("top_gainers"):
        md += "## 涨幅 TOP5\n\n"
        md += "| 名称 | 涨幅 | 行业 |\n|------|------|------|\n"
        for s in c["top_gainers"]:
            md += f"| {s['name']} | {s['pct']:+.2f}% | {s.get('industry', '')} |\n"
        md += "\n"

    if c.get("top_losers"):
        md += "## 跌幅 TOP5\n\n"
        md += "| 名称 | 跌幅 | 行业 |\n|------|------|------|\n"
        for s in c["top_losers"]:
            md += f"| {s['name']} | {s['pct']:+.2f}% | {s.get('industry', '')} |\n"
        md += "\n"

    if c.get("top_sectors"):
        md += "## 行业涨幅 TOP5\n\n"
        md += "| 行业 | 平均涨幅 |\n|------|----------|\n"
        for s in c["top_sectors"]:
            md += f"| {s['name']} | {s['avg_pct']:+.2f}% |\n"
        md += "\n"

    md += f"---\n*报告由 Stockwin 短线助手自动生成 · {dt_label}*\n"
    return md


def _build_pdf_response(date: str, cached: dict):
    from io import BytesIO
    from urllib.parse import quote

    from fpdf import FPDF

    c = cached["content"]
    dt_label = date[:4] + "-" + date[4:6] + "-" + date[6:8]

    pdf = FPDF()
    pdf.add_page()

    # Try to use a CJK font
    font_name = "Helvetica"
    font_path = None
    for cand in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]:
        import os as _os
        if _os.path.exists(cand):
            font_path = cand
            break

    if font_path:
        pdf.add_font("CJK", "", font_path, uni=True)
        pdf.add_font("CJK", "B", font_path, uni=True)
        font_name = "CJK"

    def _cjk(text, size=10, bold=False, align="L"):
        style = "B" if bold else ""
        pdf.set_font(font_name, style, size)
        pdf.multi_cell(0, size * 1.5, text, align=align)

    # Title
    _cjk(f"每日市场复盘简报 — {dt_label}", size=18, bold=True, align="C")
    pdf.ln(6)

    # Summary section
    _cjk("大盘概览", size=14, bold=True)
    pdf.ln(2)
    pdf.set_font(font_name, "", 11)
    pdf.cell(0, 8, f"全市场: {c['total']} 只", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"上涨: {c['up_count']} | 平盘: {c.get('flat_count', 0)} | 下跌: {c['down_count']}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"涨停: {c['limit_up']} 只 | 跌停: {c['limit_down']} 只", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"平均涨跌幅: {c['avg_pct']:+.2f}%", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"涨跌比: {c.get('up_ratio', 0)}%", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Table helper
    def _table(title, rows, headers, col_widths):
        _cjk(title, size=13, bold=True)
        pdf.ln(2)
        pdf.set_font(font_name, "B", 10)
        for i, h in enumerate(headers):
            pdf.cell(col_widths[i], 8, h, border=1, align="C")
        pdf.ln()
        pdf.set_font(font_name, "", 10)
        for row in rows:
            for i, val in enumerate(row):
                pdf.cell(col_widths[i], 8, str(val), border=1, align="C" if i > 0 else "L")
            pdf.ln()
        pdf.ln(4)

    if c.get("top_gainers"):
        _table("涨幅 TOP5",
               [[s['name'], f"{s['pct']:+.2f}%", s.get('industry', '')] for s in c["top_gainers"]],
               ["名称", "涨幅", "行业"],
               [60, 40, 50])

    if c.get("top_losers"):
        _table("跌幅 TOP5",
               [[s['name'], f"{s['pct']:+.2f}%", s.get('industry', '')] for s in c["top_losers"]],
               ["名称", "跌幅", "行业"],
               [60, 40, 50])

    if c.get("top_sectors"):
        _table("行业涨幅 TOP5",
               [[s['name'], f"{s['avg_pct']:+.2f}%"] for s in c["top_sectors"]],
               ["行业", "平均涨幅"],
               [80, 50])

    pdf.ln(4)
    pdf.set_font(font_name, "", 9)
    pdf.cell(0, 6, f"报告由 Stockwin 短线助手自动生成 · {dt_label}", align="C")

    buf = BytesIO()
    pdf.output(buf)
    buf.seek(0)

    filename = f"复盘报告_{date}.pdf"
    return PlainTextResponse(
        content=buf.read(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@risk_router.get("")
async def risk_list(page: int = 1, page_size: int = 20, user: dict = Depends(require_auth_optional)):
    from app.core.database import async_session
    from app.models.orm.models import RiskListResult
    from sqlalchemy import select, func

    trade_date, is_td = await _get_trade_context()
    items = []

    for offset in range(4):
        td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        cached = await cache_get(f"risk:list:{td}")
        if cached:
            items = cached
            break

    if not items:
        async with async_session() as session:
            r = await session.execute(select(func.max(RiskListResult.calc_date)))
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
            data={"total": 0, "page": page, "page_size": page_size, "items": [],
                  "is_trade_day": is_td, "trade_date": trade_date},
            timestamp=int(time.time()),
            ext_info={"note": "暂无风险扫描数据"},
        )

    total = len(items)
    start = (page - 1) * page_size
    paged = items[start:start + page_size]

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": paged,
              "is_trade_day": is_td, "trade_date": trade_date},
        timestamp=int(time.time()),
    )


# ── 交易日历 ──

@market_router.get("/calendar")
async def market_calendar(user: dict = Depends(require_auth_optional)):
    """返回当月交易日历 + 休市倒计时。"""
    today = date.today()
    today_str = today.strftime("%Y%m%d")

    # 当月范围
    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)

    # 拉到 ±30 天确保缓存覆盖
    start_str = (month_start - timedelta(days=30)).strftime("%Y%m%d")
    end_str = (month_end + timedelta(days=30)).strftime("%Y%m%d")
    trade_days = await get_trade_days_in_range(start_str, end_str)

    # 当月交易日
    month_trade_days = [d for d in trade_days if month_start.strftime("%Y%m%d") <= d <= month_end.strftime("%Y%m%d")]

    try:
        is_td = await is_trade_date(today_str)
    except Exception:
        is_td = today.weekday() < 5

    # 倒计时：找下一个交易日
    countdown = None
    next_trade_day = None
    try:
        next_trade_day = await get_next_trade_date(today_str)
        next_dt = datetime.strptime(next_trade_day, "%Y%m%d")
        delta = (next_dt.date() - today).days
        if delta > 0:
            countdown = {
                "days": delta,
                "next_date": next_trade_day,
                "label": f"距下次开盘还有 {delta} 天" if delta > 1 else "明天开盘",
            }
        elif not is_td:
            countdown = {
                "days": 0,
                "next_date": next_trade_day,
                "label": "今天休市",
            }
    except Exception:
        pass

    # 构建当月日历（简化为周视图）
    weeks = []
    current = month_start
    while current.weekday() != 0:  # 回退到周一
        current = current - timedelta(days=1)

    cursor = current
    for _ in range(6):  # 最多6周
        week = []
        for _ in range(7):
            ds = cursor.strftime("%Y%m%d")
            week.append({
                "date": cursor.day,
                "date_str": ds,
                "is_current_month": cursor.month == today.month,
                "is_trade_day": ds in trade_days,
                "is_today": ds == today_str,
                "is_weekend": cursor.weekday() >= 5,
            })
            cursor += timedelta(days=1)
        weeks.append(week)
        if cursor > month_end and cursor.weekday() == 0:
            break

    trade_date, _ = await _get_trade_context()

    return APIResponse(data={
        "month": f"{today.year}-{today.month:02d}",
        "is_trade_day": is_td,
        "today": today_str,
        "trade_date": trade_date,
        "month_trade_days": len(month_trade_days),
        "weeks": weeks,
        "countdown": countdown,
    }, timestamp=int(time.time()))


# ── 行业龙头成分股 ──

@market_router.get("/industry-leaders")
async def industry_leaders(ts_code: str = "", user: dict = Depends(require_auth_optional)):
    """根据股票代码查询同行业市值Top5龙头股。"""
    if not ts_code:
        return APIResponse(data={"industry": "", "leaders": []}, timestamp=int(time.time()))

    from app.core.database import async_session

    code = ts_code.strip()
    if "." not in code:
        for suffix in [".SZ", ".SH"]:
            c = code + suffix
            async with async_session() as sess:
                r = await sess.execute(
                    text("SELECT industry FROM stocks WHERE ts_code=:c"),
                    {"c": c},
                )
                row = r.first()
                if row:
                    code = c
                    break

    industry_name = ""
    stock_name = code
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT name, industry FROM stocks WHERE ts_code=:c"),
            {"c": code},
        )
        row = r.first()
        if not row:
            return APIResponse(data={"industry": "", "leaders": [], "stock_code": code}, timestamp=int(time.time()))
        stock_name, industry_name = row[0], row[1]

    if not industry_name:
        return APIResponse(data={"industry": "", "leaders": [], "stock_code": code}, timestamp=int(time.time()))

    cache_key = f"industry:leaders:{industry_name}"
    cached = await cache_get(cache_key)
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    # 获取同行业所有股票代码
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT ts_code FROM stocks WHERE industry=:ind"),
            {"ind": industry_name},
        )
        industry_codes = [row2[0] for row2 in r]

    if not industry_codes:
        return APIResponse(data={"industry": industry_name, "leaders": [], "stock_code": code}, timestamp=int(time.time()))

    trade_date, _ = await _get_trade_context()

    # 从 tushare daily_basic 获取总市值数据（按日尝试，取最近有数据的）
    leaders_raw = []
    from app.services.tushare_client import get_daily_basic
    for offset in range(7):
        td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            basics = await get_daily_basic(td)
            if basics:
                code_set = set(industry_codes)
                for b in basics:
                    tc = b.get("ts_code", "")
                    if tc in code_set:
                        leaders_raw.append({
                            "ts_code": tc,
                            "total_mv": float(b.get("total_mv", 0) or 0),
                            "circ_mv": float(b.get("circ_mv", 0) or 0),
                            "pe": float(b.get("pe", 0) or 0),
                            "pb": float(b.get("pb", 0) or 0),
                        })
                if leaders_raw:
                    break
        except Exception:
            continue

    # 按总市值降序，取Top5
    leaders_raw.sort(key=lambda x: x["total_mv"], reverse=True)
    top5 = leaders_raw[:5]

    # 补充名称和最新价
    leaders = []
    for item in top5:
        async with async_session() as sess:
            r = await sess.execute(
                text("SELECT name FROM stocks WHERE ts_code=:c"),
                {"c": item["ts_code"]},
            )
            name_row = r.first()
            name = name_row[0] if name_row else item["ts_code"]
            r2 = await sess.execute(
                text("SELECT close, pct_chg FROM stock_daily WHERE ts_code=:c AND trade_date=:td ORDER BY trade_date DESC LIMIT 1"),
                {"c": item["ts_code"], "td": trade_date},
            )
            dr = r2.first()
            close = round(float(dr[0]), 2) if dr and dr[0] else 0
            pct_chg = round(float(dr[1]), 2) if dr and dr[1] else 0

        leaders.append({
            "ts_code": item["ts_code"],
            "name": name,
            "close": close,
            "pct_chg": pct_chg,
            "total_mv": round(item["total_mv"] / 1e4, 2),
            "circ_mv": round(item["circ_mv"] / 1e4, 2),
            "pe": round(item["pe"], 2),
            "pb": round(item["pb"], 2),
        })

    data = {
        "industry": industry_name,
        "stock_name": stock_name,
        "stock_code": code,
        "leaders": leaders,
        "trade_date": trade_date,
    }
    ttl = 300 if _is_trading_time() else 86400
    await cache_set(cache_key, data, ttl=ttl)
    return APIResponse(data=data, timestamp=int(time.time()))
