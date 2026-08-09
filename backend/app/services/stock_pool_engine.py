"""选股池计算引擎——4大策略离线批量计算，基于SQLite真实数据。

收盘后运行一次，结果写入内存缓存。每池15只，互不重复。
所有跨日查询均使用实际可用的交易日期，不硬编码天数偏移。
2026-08-10: 因子评分升级 — 统一使用 factor_weights.json 配置驱动的 score_and_rank()
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings
from app.services.factor_lib import score_and_rank

logger = logging.getLogger("stock_pool")
_settings = get_settings()
_WEIGHTS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "factor_weights.json"

POOL_SIZE = 15


class StockPoolEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        # 加载因子权重配置
        with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            fw = json.load(f)

        if not trade_date:
            async with async_session() as sess:
                r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
                trade_date = r.scalar() or (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        base_dt = datetime.strptime(trade_date, "%Y%m%d")
        target_td5 = (base_dt - timedelta(days=7)).strftime("%Y%m%d")
        target_start20 = (base_dt - timedelta(days=30)).strftime("%Y%m%d")

        async with async_session() as sess:
            # 找 td5：target_td5 之前最近的交易日（>=50只股票）
            r = await sess.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :t "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ), {"t": target_td5})
            td5_row = r.fetchone()
            td5 = td5_row[0] if td5_row else trade_date

            # 找 start20：target_start20 附近最近交易日
            r = await sess.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :t AND trade_date >= :floor "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ), {"t": target_start20, "floor": target_td5})
            s20_row = r.fetchone()
            start20 = s20_row[0] if s20_row else td5

        # 找出全部交易日的60日窗口（给需要mvd的池用）
        async with async_session() as sess:
            r = await sess.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60"
            ), {"td": trade_date})
            all_dates = [row[0] for row in r.fetchall()]

        async with async_session() as session:
            hot = await self._hot_leader(session, trade_date, all_dates, fw)
            used = {s["stock_code"] for s in hot if s["stock_code"]}

            dip = await self._dip_ambush(session, trade_date, start20, used, fw)
            used.update(s["stock_code"] for s in dip if s["stock_code"])

            bounce = await self._oversold_rebound(session, trade_date, td5, used, fw)
            used.update(s["stock_code"] for s in bounce if s["stock_code"])

            steady = await self._steady_swing(session, trade_date, used, fw)

        pools = {
            "hot_leader": hot, "dip_ambush": dip,
            "oversold_rebound": bounce, "steady_swing": steady,
        }
        for ptype in pools:
            await cache_set(f"pool:{ptype}:{trade_date}", pools[ptype],
                            ttl=_settings.cache_offline_ttl)
            await self._persist_pool(ptype, pools[ptype], trade_date)

        counts = {k: len(v) for k, v in pools.items()}
        logger.info(f"Stock pools computed for {trade_date}: {counts}")
        return pools

    # ── 日期辅助 ──

    def _nth_date(self, dates: list, ref_date: str, n: int) -> str | None:
        if not dates:
            return None
        filtered = [d for d in dates if d <= ref_date]
        filtered.sort(reverse=True)
        if len(filtered) > n:
            return filtered[n]
        return filtered[-1] if filtered else None

    # ── 热点龙头池 ──

    # ── 热点龙头池 ──

    async def _hot_leader(self, session, trade_date: str, dates: list, fw: dict) -> list:
        td3 = self._nth_date(dates, trade_date, 3)
        td2 = self._nth_date(dates, trade_date, 2)
        if not td3:
            return []

        # Cols: ts_code, name, close, pct_chg, vol, chg3, vol_ratio, turnover, pe, pb, total_mv
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d3.close) / NULLIF(d3.close, 0) * 100 AS chg3,
                   CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) AS vol_ratio,
                   CASE WHEN db.turnover_rate IS NOT NULL AND db.turnover_rate > 0 THEN db.turnover_rate ELSE NULL END AS turnover,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td3) d3 ON d3.ts_code = d.ts_code
            JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                  WHERE trade_date IN (:td3,:td,:td2) GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.pct_chg > 3
              AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td3": td3, "td2": td2 or trade_date, "lim": POOL_SIZE * 3})
        return score_and_rank(r.fetchall(), fw["hot_leader"], "热点龙头", limit=POOL_SIZE)

    # ── 低吸埋伏池 ──

    async def _dip_ambush(self, session, trade_date: str, start20: str, exclude: set, fw: dict) -> list:
        td5 = self._nth_date([trade_date], trade_date, 5)  # 回退 5 天取均量
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - low20.min_low) / NULLIF(low20.min_low, 0) * 100 AS dist_from_low,
                   CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) AS vol_ratio,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, MIN(low) AS min_low FROM stock_daily
                WHERE trade_date <= :td AND trade_date >= :start20 GROUP BY ts_code
            ) low20 ON low20.ts_code = d.ts_code
            LEFT JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                       WHERE trade_date <= :td AND trade_date >= :td5 GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.close > 0 AND low20.min_low > 0
              AND d.pct_chg > -3 AND d.pct_chg < 2
              AND d.volume > 0
              AND d.close < low20.min_low * 1.10
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "start20": start20, "td5": td5 or start20, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["dip_ambush"], "低吸埋伏", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 超跌反弹池 ──

    async def _oversold_rebound(self, session, trade_date: str, td5: str, exclude: set, fw: dict) -> list:
        result = await self._try_oversold_rebound(session, trade_date, td5, exclude, fw)
        if len(result) < 10:
            fallback_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=5)).strftime("%Y%m%d")
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :fb "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ), {"fb": fallback_date})
            fb_row = r.fetchone()
            if fb_row and str(fb_row[0]) != str(td5):
                fallback_result = await self._try_oversold_rebound(session, trade_date, str(fb_row[0]), exclude, fw)
                if len(fallback_result) > len(result):
                    result = fallback_result
        return result

    async def _try_oversold_rebound(self, session, trade_date: str, td5: str, exclude: set, fw: dict) -> list:
        td2 = self._nth_date([trade_date], trade_date, 2)
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d5.close) / NULLIF(d5.close, 0) * 100 AS chg5,
                   CASE WHEN av.avg_vol > 0 THEN CAST(d.volume AS REAL) / av.avg_vol ELSE NULL END AS vol_ratio,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, close FROM stock_daily WHERE trade_date = :td5
            ) d5 ON d5.ts_code = d.ts_code
            LEFT JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                       WHERE trade_date <= :td AND trade_date >= :td5 GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
              AND (d.close - d5.close) / NULLIF(d5.close, 0) * 100 < -5
              AND d.pct_chg > -4
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td5": td5, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["oversold_rebound"], "超跌反弹", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 稳健波段池 ──

    async def _steady_swing(self, session, trade_date: str, exclude: set, fw: dict) -> list:
        td5 = self._nth_date([trade_date], trade_date, 5)
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   CAST(d.volume AS REAL) / NULLIF(avg.avg_vol, 0) AS vol_ratio,
                   (d.close - d5.close) / NULLIF(d5.close, 0) * 100 AS chg5,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                WHERE trade_date < :td GROUP BY ts_code
            ) avg ON avg.ts_code = d.ts_code
            LEFT JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td5) d5 ON d5.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
              AND d.pct_chg BETWEEN 0.5 AND 6
              AND d.volume > 0
              AND d.close > d.open
              AND CAST(d.volume AS REAL) / avg.avg_vol BETWEEN 0.5 AND 3.0
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td5": td5 or trade_date, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["steady_swing"], "稳健波段", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 持久化 ──

    async def _persist_pool(self, pool_type: str, items: list, calc_date: str):
        async with async_session() as session:
            for i, item in enumerate(items):
                try:
                    await session.execute(text("""
                        INSERT OR REPLACE INTO stock_pool_results
                            (calc_date, pool_type, rank_in_pool, ts_code, stock_name,
                             market_data_json, inclusion_reason)
                        VALUES (:cd, :pt, :rk, :ts, :nm, :md, :ir)
                    """), {
                        "cd": calc_date,
                        "pt": pool_type,
                        "rk": i + 1,
                        "ts": item.get("stock_code", ""),
                        "nm": item.get("stock_name", ""),
                        "md": json.dumps({
                            "close": item.get("close"),
                            "change_pct": item.get("change_pct"),
                            "volume_ratio": item.get("volume_ratio"),
                            "score": item.get("score"),
                        }),
                        "ir": item.get("inclusion_reason", ""),
                    })
                except Exception:
                    continue
            await session.commit()
