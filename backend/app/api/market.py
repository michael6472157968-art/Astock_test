"""市场行情 API——板块分析、每日复盘、风险避雷、指数行情、市场情绪。"""

from __future__ import annotations

import asyncio
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
from app.services.tushare_client import get_index_daily
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


_trade_ctx_lock = asyncio.Lock()


async def _get_trade_context() -> tuple[str, bool]:
    """返回 (trade_date, is_trade_day) — 统一入口，所有端点共用。

    优先级：stock_daily MAX(trade_date) → get_latest_trade_date() → date.today()
    60 秒内存缓存 + 互斥锁，避免并发请求同时查 DB。
    """
    from app.core.database import async_session
    from sqlalchemy import text

    now_ts = time.time()
    global _trade_ctx_cache
    if _trade_ctx_cache and (now_ts - _trade_ctx_cache[2]) < 60:
        return _trade_ctx_cache[0], _trade_ctx_cache[1]

    async with _trade_ctx_lock:
        if _trade_ctx_cache and (time.time() - _trade_ctx_cache[2]) < 60:
            return _trade_ctx_cache[0], _trade_ctx_cache[1]

    today_str = date.today().strftime("%Y%m%d")
    trade_date = today_str

    try:
        async with async_session() as sess:
            # 完整交易日(>=50只)优先，避免盘中 MAX 跳到残缺新交易日
            r = await sess.execute(text(
                "SELECT trade_date FROM stock_daily GROUP BY trade_date "
                "HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ))
            max_td = r.scalar()
            if max_td:
                trade_date = max_td
    except Exception:
        pass

    if trade_date == today_str:
        try:
            trade_date = await get_latest_trade_date()
        except Exception:
            pass

    try:
        is_td_val = await is_trade_date(today_str)
    except Exception:
        is_td_val = date.today().weekday() < 5

    _trade_ctx_cache = (trade_date, is_td_val, now_ts)
    return trade_date, is_td_val


_trade_ctx_cache = None


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


@market_router.get("/index_kline")
async def market_index_kline(code: str = "000001.SH", days: int = 120):
    """指数历史K线——支持4大指数切换，优先从Tushare拉取后缓存。"""
    from app.core.database import async_session

    VALID_CODES = {c: n for c, n in INDEX_CODES}
    if code not in VALID_CODES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"不支持的指数代码: {code}")

    name = VALID_CODES[code]
    trade_date, _ = await _get_trade_context()

    cache_key = f"index_kline:{code}:{trade_date}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

    kline = []
    try:
        end_date = (date.today()).strftime("%Y%m%d")
        start_date = (date.today() - timedelta(days=days + 10)).strftime("%Y%m%d")
        rows = await get_index_daily(code, start_date, end_date)
        if rows:
            for r in rows:
                kline.append({
                    "date": str(r.get("trade_date", "")),
                    "open": round(float(r.get("open", 0) or 0), 2),
                    "high": round(float(r.get("high", 0) or 0), 2),
                    "low": round(float(r.get("low", 0) or 0), 2),
                    "close": round(float(r.get("close", 0) or 0), 2),
                    "volume": int(float(r.get("vol", 0) or 0)),
                })
            kline.sort(key=lambda x: x["date"])
    except Exception as e:
        logger.warning(f"index_kline({code}): {e}")

    result = {
        "code": code,
        "name": name,
        "kline": kline,
        "source": "tushare" if kline else "none",
        "trade_date": trade_date,
    }
    if kline:
        await cache_set(cache_key, result, ttl=86400)
    return APIResponse(data=result, timestamp=int(time.time()))


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
            # Top 8 by pct_chg desc（按涨幅排序，涨得多的在前）
            top8 = sorted(industries, key=lambda x: x[1], reverse=True)[:8]
            result["top_sectors"] = [{"name": n, "pct_chg": pct, "cnt": c} for n, pct, c in top8]

    # 3. 北向资金（DB优先——定时任务已同步到moneyflow_hsgt表）
    try:
        r3 = await sess.execute(text(
            "SELECT trade_date, north_money, ggt_ss, ggt_sz, hgt, sgt "
            "FROM moneyflow_hsgt ORDER BY trade_date DESC LIMIT 5"
        ))
        hsgt_rows = list(r3)
        if hsgt_rows:
            latest = hsgt_rows[0]
            result["northbound"] = {
                "date": latest[0],
                "net_in": round(float(latest[1] or 0) * 1e4, 2),
                "ggt_ss": round(float(latest[2] or 0), 2),
                "ggt_sz": round(float(latest[3] or 0), 2),
                "hgt": round(float(latest[4] or 0), 2),
                "sgt": round(float(latest[5] or 0), 2),
            }
            result["northbound"]["recent"] = [
                {"date": r[0], "net_in": round(float(r[1] or 0) * 1e4, 2)}
                for r in hsgt_rows
            ][::-1]
    except Exception:
        pass

    # 北向DB无数据时降级调用Tushare实时API
    if not result.get("northbound"):
        try:
            from app.services.tushare_client import get_moneyflow_hsgt
            end_d = date.today().strftime("%Y%m%d")
            start_d = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
            nf = await get_moneyflow_hsgt(start_d, end_d)
            if nf:
                nf_sorted = sorted(nf, key=lambda x: x.get("trade_date", ""), reverse=True)
                latest = nf_sorted[0]
                nb_date = latest.get("trade_date", end_d)
                north_val = float(latest.get("north_money", 0) or 0) * 1e4
                result["northbound"] = {
                    "date": nb_date,
                    "net_in": round(north_val, 2),
                    "ggt_ss": round(float(latest.get("ggt_ss", 0) or 0), 2),
                    "ggt_sz": round(float(latest.get("ggt_sz", 0) or 0), 2),
                    "hgt": round(float(latest.get("hgt", 0) or 0), 2),
                    "sgt": round(float(latest.get("sgt", 0) or 0), 2),
                }
                result["northbound"]["recent"] = [
                    {"date": x.get("trade_date", ""), "net_in": round(float(x.get("north_money", 0) or 0) * 1e4, 2)}
                    for x in nf_sorted[:5]
                ][::-1]
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
                    text("SELECT COUNT(*) FILTER (WHERE limit_type = 'U') as up, "
                         "COUNT(*) FILTER (WHERE limit_type = 'D') as down "
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

                # 主力资金净流入（全市场聚合，单位万元）
                mf_r = await sess.execute(
                    text("SELECT SUM(net_mf_amount) FROM moneyflow_records WHERE trade_date = :td"),
                    {"td": td},
                )
                main_net_inflow = round(float(mf_r.scalar() or 0), 2)

                # 平均获利盘（筹码 winner_rate，0-100）
                cyq_r = await sess.execute(
                    text("SELECT AVG(winner_rate) FROM cyq_perf WHERE trade_date = :td"),
                    {"td": td},
                )
                avg_winner_rate = round(float(cyq_r.scalar() or 0), 1)

                mood_data = {
                    "up": int(row[0]), "down": int(row[1]), "flat": int(row[2]),
                    "limit_up": limit_up, "limit_down": limit_down,
                    "main_net_inflow": main_net_inflow,
                    "avg_winner_rate": avg_winner_rate,
                    "date": td, "source": "tushare", "update_time": now_ts,
                    "is_trade_day": is_td, "trade_date": trade_date,
                }
                break

    if not mood_data:
        return APIResponse(
            data={"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
                  "main_net_inflow": 0, "avg_winner_rate": 0,
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
        "source": "db_industry",
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
        await cache_set(f"sector:ranking:{trade_date}", sectors, ttl=86400)
        return APIResponse(data={"date": trade_date, "sectors": sectors}, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "sectors": []}, timestamp=int(time.time()),
                       ext_info={"note": "请先在管理后台执行数据同步和板块分析计算"})


def _judge_stage(c20: float, c5: float) -> str:
    """根据 20日涨幅 + 5日涨幅 判断生命周期阶段。"""
    if c20 > 10 and c5 > 0:
        return "主升"
    elif c20 > 10 and c5 < -3:
        return "见顶"
    elif c5 > 3 and c20 < 10:
        return "启动"
    elif c5 < -3 and c20 < 0:
        return "下行"
    else:
        return "震荡"


@sector_router.get("/rotation")
async def sector_rotation(days: int = 20, user: dict = Depends(require_auth_optional)):
    """申万二级行业轮动：生命周期阶段热力图数据(每个行业每天的阶段)。"""
    trade_date, _ = await _get_trade_context()
    cache_key = f"sector:rotation:{trade_date}:{days}"
    cached = await cache_get(cache_key)
    if cached:
        return APIResponse(data=cached, timestamp=int(time.time()))

    from app.core.database import async_session
    from sqlalchemy import text
    from app.services.tushare_client import call_tushare

    # 1. 申万二级行业列表（静态，长缓存）
    l2_key = "sw_l2_sectors"
    l2_map = await cache_get(l2_key)
    if not l2_map:
        try:
            cls = await call_tushare("index_classify", src="SW2021", level="L2")
            if cls is not None and not cls.empty:
                l2_map = {r["index_code"]: r["industry_name"] for r in cls.to_dict("records")}
                await cache_set(l2_key, l2_map, ttl=86400 * 30)
        except Exception:
            l2_map = {}
    if not l2_map:
        return APIResponse(data={"date": trade_date, "days": days, "dates": [], "sectors": [], "heatmap": []},
                           timestamp=int(time.time()))

    # 2. 拉近 (days+20) 日数据（多20日用于算滚动阶段）
    async with async_session() as sess:
        r = await sess.execute(text(
            "SELECT DISTINCT trade_date FROM sector_daily ORDER BY trade_date DESC LIMIT :n"
        ), {"n": days})
        display_dates = sorted([row[0] for row in r.fetchall()])
        if len(display_dates) < 10:
            return APIResponse(data={"date": trade_date, "days": days, "dates": [], "sectors": [], "cum_heatmap": []},
                               timestamp=int(time.time()))
        date_idx = {d: i for i, d in enumerate(display_dates)}

        r2 = await sess.execute(text(
            "SELECT code, trade_date, pct_chg FROM sector_daily "
            "WHERE trade_date >= :start AND trade_date <= :end ORDER BY code, trade_date"
        ), {"start": display_dates[0], "end": display_dates[-1]})
        daily: dict = {}
        for code, td, pct in r2:
            daily.setdefault(code, []).append((td, pct))

    # 3. 算每个行业的累计涨幅序列（从起点 display_dates[0] 累乘到每天）
    raw = []
    for code, name in sorted(l2_map.items()):
        series = daily.get(code, [])
        if not series:
            continue
        cum = 1.0
        cum_by_td = {}
        for td, p in series:
            cum *= (1 + p / 100)
            cum_by_td[td] = round((cum - 1) * 100, 2)
        cur_cum = cum_by_td.get(display_dates[-1], 0) if display_dates else 0
        raw.append({"code": code, "name": name, "cum": cur_cum, "cum_by_td": cum_by_td})

    # 4. 按当前累计涨幅排序
    raw.sort(key=lambda x: -x["cum"])
    sectors = [{"code": r["code"], "name": r["name"], "cum": r["cum"]} for r in raw]
    cum_heatmap = []
    for i, r in enumerate(raw):
        for d in display_dates:
            if d in r["cum_by_td"]:
                cum_heatmap.append([date_idx[d], i, r["cum_by_td"][d]])

    data = {"date": trade_date, "days": days, "dates": display_dates, "sectors": sectors, "cum_heatmap": cum_heatmap}
    await cache_set(cache_key, data, ttl=86400)
    return APIResponse(data=data, timestamp=int(time.time()))


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


@review_router.get("/rule")
async def review_rule(date: str = "", user: dict = Depends(require_auth_optional)):
    """7步纯规则复盘（不用AI）。"""
    from app.services.review_rule import compute_review
    result = await compute_review(date or "")
    return APIResponse(data=result, timestamp=int(time.time()))


@review_router.get("/pool-performance")
async def review_pool_performance(user: dict = Depends(require_auth_optional)):
    """选股池次日收益：昨天选的股票今天平均涨幅。"""
    from app.services.review_rule import pool_performance
    result = await pool_performance()
    return APIResponse(data=result, timestamp=int(time.time()))


@review_router.get("/latest")
async def latest_review(user: dict = Depends(require_auth_optional)):
    trade_date, is_td = await _get_trade_context()
    cached = await cache_get(f"review:{trade_date}")
    if cached:
        cached["is_trade_day"] = is_td
        cached["trade_date"] = trade_date
        return APIResponse(data=cached, timestamp=int(time.time()))

    # 缓存清空后回退计算
    from app.services.market_review import MarketReviewEngine
    engine = MarketReviewEngine()
    review_data = await engine.compute(trade_date)
    if review_data and (review_data.get("content", {}).get("temperature", {}).get("total") or review_data.get("content", {}).get("ai_summary")):
        await cache_set(f"review:{trade_date}", review_data, ttl=86400 * 60)
        review_data["is_trade_day"] = is_td
        review_data["trade_date"] = trade_date
        return APIResponse(data=review_data, timestamp=int(time.time()))

    return APIResponse(data={"date": "", "content": {}, "is_trade_day": is_td, "trade_date": trade_date}, timestamp=int(time.time()),
                       ext_info={"note": "需要先运行数据同步"})


@review_router.get("/dates")
async def review_dates(user: dict = Depends(require_auth_optional)):
    """Return dates that have trade data in stock_daily (descending, last 60 days)."""
    from app.core.database import async_session

    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT trade_date FROM stock_daily GROUP BY trade_date "
                 "HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60")
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
            return APIResponse(data=cached, timestamp=int(time.time()))
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
    if review_data and (review_data.get("content", {}).get("temperature", {}).get("total") or review_data.get("content", {}).get("ai_summary")):
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

    content = review_data.get("content", {})
    temp = content.get("temperature", {})
    if not review_data or (not temp.get("total") and not content.get("ai_summary")):
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

    if not cached or not cached.get("content"):
        raise HTTPException(status_code=404, detail="报告不存在或数据为空")
    temp = cached.get("content", {}).get("temperature", {})
    if not temp.get("total") and not cached.get("content", {}).get("ai_summary"):
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
    md = f"# AI 量化每日复盘 — {dt_label}\n\n"

    # Dimension 1: Market temperature
    t = c.get("temperature", {})
    if t and t.get("total"):
        md += f"## 大盘温度 — {t.get('width_label', '')}\n\n"
        md += f"- 全市场: **{t['total']}** 只\n"
        md += f"- 上涨: **{t.get('up_count', 0)}** | 平盘: **{t.get('flat_count', 0)}** | 下跌: **{t.get('down_count', 0)}**\n"
        md += f"- 涨停: **{t.get('limit_up', 0)}** 只 | 跌停: **{t.get('limit_down', 0)}** 只\n"
        md += f"- 平均涨跌幅: **{t.get('avg_pct', 0):+.2f}%**\n"
        md += f"- 涨跌比: **{t.get('up_ratio', 0)}%**\n"
        md += f"- 成交额: **{t.get('total_amount_yi', 0):.0f}** 亿 | 均换手: **{t.get('avg_turnover', 0)}%**\n"
        md += f"- 情绪分: **{t.get('sentiment_score', '')}**\n\n"

        if t.get("top_gainers"):
            md += "### 涨幅 TOP5\n\n| 名称 | 涨幅 | 行业 |\n|------|------|------|\n"
            for s in t["top_gainers"]:
                md += f"| {s['name']} | {s['pct']:+.2f}% | {s.get('industry', '')} |\n"
            md += "\n"
        if t.get("top_losers"):
            md += "### 跌幅 TOP5\n\n| 名称 | 跌幅 | 行业 |\n|------|------|------|\n"
            for s in t["top_losers"]:
                md += f"| {s['name']} | {s['pct']:+.2f}% | {s.get('industry', '')} |\n"
            md += "\n"
        if t.get("top_sectors"):
            md += "### 行业涨幅 TOP5\n\n| 行业 | 平均涨幅 |\n|------|----------|\n"
            for s in t["top_sectors"]:
                md += f"| {s['name']} | {s['avg_pct']:+.2f}% |\n"
            md += "\n"

    # Dimension 2: Smart money
    sm = c.get("smart_money", {})
    if sm and sm.get("smart_money_label"):
        md += f"## 聪明钱共识 — {sm.get('smart_money_label', '')}\n\n"
        md += f"- 北向净流入: **{sm.get('northbound_net_yi', 0):+.1f}** 亿\n"
        if sm.get("northbound_5d_trend"):
            trend_str = " → ".join(f"{v:+.1f}" for v in sm["northbound_5d_trend"])
            md += f"- 北向5日趋势: {trend_str} 亿\n"
        md += f"- 融资余额变化: **{sm.get('margin_balance_change_yi', 0):+.1f}** 亿\n"
        md += f"- 融资余额总量: **{sm.get('margin_balance_total_yi', 0):.0f}** 亿\n\n"

    # Dimension 3: Limit-up deep
    lu = c.get("limit_up_deep", {})
    if lu and lu.get("total_limit_up", 0) > 0:
        md += f"## 涨停深度复盘\n\n"
        md += f"- 涨停: **{lu.get('total_limit_up', 0)}** | 炸板: **{lu.get('zhaban', 0)}** | 封板率: **{lu.get('seal_rate', 0)}%**\n"
        md += f"- 首板: **{lu.get('first_board', 0)}** | 连板: **{lu.get('lianban_count', 0)}**\n"
        if lu.get("lianban_king"):
            k = lu["lianban_king"]
            md += f"- 连板龙头: **{k.get('name', '')}** ({k.get('boards', 0)}板) +{k.get('pct_chg', 0)}%\n"
        if lu.get("top_concepts"):
            md += f"- 热门概念: {' / '.join(lu['top_concepts'])}\n"
        md += "\n"

    # Dimension 4: Earnings bombs
    eb = c.get("earnings_bombs", {})
    if eb:
        warnings = eb.get("warnings", [])
        md += f"## 业绩预警 ({len(warnings)}条)\n\n"
        if warnings:
            md += "| 名称 | 预警类型 | PE | 涨跌 |\n|------|---------|----|------|\n"
            for w in warnings:
                md += f"| {w.get('name', '')} | {w.get('type', '')} | {w.get('pe', '-')} | {w.get('pct_chg', 0):+.2f}% |\n"
            md += "\n"
        if eb.get("note"):
            md += f"*{eb['note']}*\n\n"

    # Dimension 6: Anomaly signals
    an = c.get("anomaly", {})
    if an:
        md += f"## 异常信号\n\n"
        md += f"- 高换手(>10%): **{len(an.get('high_turnover', []))}** 只\n"
        md += f"- 放量(量比>3): **{len(an.get('volume_surge', []))}** 只\n"
        md += f"- 5日急涨(>20%): **{len(an.get('rapid_rise_5d', []))}** 只\n"
        md += f"- 5日急跌(<-20%): **{len(an.get('rapid_drop_5d', []))}** 只\n\n"

    # Dimension 8: AI summary
    ai = c.get("ai_summary", {})
    if ai and ai.get("text"):
        md += f"## AI 总结\n\n> {ai['text']}\n\n"
        md += f"*生成模型: {ai.get('model', 'AI')}*\n\n"

    md += f"---\n*报告由 Stockwin AI量化复盘自动生成 · {dt_label}*\n"
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
    """返回当月交易日历 + 休市倒计时（缓存1小时）。"""
    today = date.today()
    cache_key = f"market:calendar:{today.strftime('%Y%m')}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

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

    # 获取当月历史交易日的大盘涨跌数据，用于日历着色
    index_pct_map = {}
    try:
        cal_start = month_start
        while cal_start.weekday() != 0:
            cal_start = cal_start - timedelta(days=1)
        cal_end = month_end + timedelta(days=(6 - month_end.weekday()))
        # DB 优先——stock_daily 由 sync_index_daily 定时同步上证指数，避免每次调 Tushare
        from app.core.database import async_session as _cal_sess
        async with _cal_sess() as _s:
            _r = await _s.execute(
                text("SELECT trade_date, pct_chg FROM stock_daily "
                     "WHERE ts_code='000001.SH' AND trade_date BETWEEN :s AND :e"),
                {"s": cal_start.strftime("%Y%m%d"), "e": cal_end.strftime("%Y%m%d")},
            )
            for _row in _r:
                index_pct_map[str(_row[0])] = round(float(_row[1] or 0), 2)
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
            day_data = {
                "date": cursor.day,
                "date_str": ds,
                "is_current_month": cursor.month == today.month,
                "is_trade_day": ds in trade_days,
                "is_today": ds == today_str,
                "is_weekend": cursor.weekday() >= 5,
            }
            if ds in index_pct_map:
                day_data["index_pct"] = index_pct_map[ds]
                day_data["is_trade_day"] = True  # stock_daily 有数据=必然是历史交易日
            week.append(day_data)
            cursor += timedelta(days=1)
        weeks.append(week)
        if cursor > month_end and cursor.weekday() == 0:
            break

    trade_date, _ = await _get_trade_context()

    data = {
        "month": f"{today.year}-{today.month:02d}",
        "is_trade_day": is_td,
        "today": today_str,
        "trade_date": trade_date,
        "month_trade_days": len(month_trade_days),
        "weeks": weeks,
        "countdown": countdown,
    }
    await cache_set(cache_key, data, ttl=3600)
    return APIResponse(data=data, timestamp=int(time.time()))


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

    # 从 daily_basic 表读取同行业市值数据（定时任务已同步）
    leaders_raw = []
    async with async_session() as sess:
        for offset in range(4):
            td = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                result = await sess.execute(text(
                    "SELECT ts_code, total_mv, circ_mv, pe, pb FROM daily_basic "
                    "WHERE trade_date = :td AND ts_code IN (SELECT ts_code FROM stocks WHERE industry = :ind)",
                ), {"td": td, "ind": industry_name})
                for row in result:
                    leaders_raw.append({
                        "ts_code": row[0],
                        "total_mv": float(row[1] or 0),
                        "circ_mv": float(row[2] or 0),
                        "pe": float(row[3] or 0),
                        "pb": float(row[4] or 0),
                    })
                if leaders_raw:
                    break
            except Exception:
                continue

    # DB降级：daily_basic 表无数据时实时调 Tushare
    if not leaders_raw:
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


@market_router.get("/site-config")
async def public_site_config():
    """公开站点配置——无需登录，供前端QR码生成使用。"""
    from app.core.cache import cache_get
    config = await cache_get("admin:site_config") or {"site_url": ""}
    return APIResponse(data=config, timestamp=int(time.time()))
