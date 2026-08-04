"""选股池计算引擎——4大策略离线批量计算，基于SQLite真实数据。

收盘后运行一次，结果写入内存缓存。每池15只，互不重复。
所有跨日查询均使用实际可用的交易日期，不硬编码天数偏移。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings
from app.models.orm.models import StockPoolResult

logger = logging.getLogger("stock_pool")
_settings = get_settings()

POOL_SIZE = 15


class StockPoolEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
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

        async with async_session() as session:
            hot = await self._hot_leader(session, trade_date)
            used = {s["stock_code"] for s in hot if s["stock_code"]}

            dip = await self._dip_ambush(session, trade_date, start20, used)
            used.update(s["stock_code"] for s in dip if s["stock_code"])

            bounce = await self._oversold_rebound(session, trade_date, td5, used)
            used.update(s["stock_code"] for s in bounce if s["stock_code"])

            steady = await self._steady_swing(session, trade_date, used)

        pools = {
            "hot_leader": hot, "dip_ambush": dip,
            "oversold_rebound": bounce, "steady_swing": steady,
        }
        for ptype in pools:
            await cache_set(f"pool:{ptype}:{trade_date}", pools[ptype],
                            ttl=_settings.cache_offline_ttl)
            # 持久化到 DB——缓存 miss 时可降级读取
            await self._persist_pool(ptype, pools[ptype], trade_date)

        counts = {k: len(v) for k, v in pools.items()}
        logger.info(f"Stock pools computed for {trade_date}: {counts}")
        return pools

    async def _hot_leader(self, session, trade_date: str) -> list:
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND d.pct_chg > 3
              AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%'
              AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%'
              AND d.ts_code NOT LIKE '920%'
            ORDER BY d.pct_chg DESC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "放量突破，涨幅")

    async def _dip_ambush(self, session, trade_date: str, start20: str, exclude: set) -> list:
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume, low20.min_low
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, MIN(low) AS min_low FROM stock_daily
                WHERE trade_date <= :td AND trade_date >= :start20 GROUP BY ts_code
            ) low20 ON low20.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND d.close > 0 AND low20.min_low > 0
              AND d.pct_chg > -3 AND d.pct_chg < 2
              AND d.volume > 0
              AND d.close < low20.min_low * 1.10
              AND d.ts_code NOT LIKE '%ST%'
              AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%'
              AND d.ts_code NOT LIKE '920%'
            ORDER BY d.volume ASC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "start20": start20, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "回调企稳", exclude)

    async def _oversold_rebound(self, session, trade_date: str, td5: str, exclude: set) -> list:
        result = await self._try_oversold_rebound(session, trade_date, td5, exclude)
        if len(result) < 10:
            fallback_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=5)).strftime("%Y%m%d")
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :fb "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ), {"fb": fallback_date})
            fb_row = r.fetchone()
            if fb_row and str(fb_row[0]) != str(td5):
                fallback_result = await self._try_oversold_rebound(session, trade_date, str(fb_row[0]), exclude)
                if len(fallback_result) > len(result):
                    result = fallback_result
        return result

    async def _try_oversold_rebound(self, session, trade_date: str, td5: str, exclude: set) -> list:
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d5.close) / NULLIF(d5.close, 0) * 100 AS chg5
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, close FROM stock_daily WHERE trade_date = :td5
            ) d5 ON d5.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND d.ts_code NOT LIKE '%ST%'
              AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%'
              AND d.ts_code NOT LIKE '920%'
              AND (d.close - d5.close) / NULLIF(d5.close, 0) * 100 < -5
              AND d.pct_chg > -4
            ORDER BY (d.close - d5.close) / NULLIF(d5.close, 0) * 100 ASC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td5": td5, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "超跌反弹", exclude)

    async def _steady_swing(self, session, trade_date: str, exclude: set) -> list:
        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   CAST(d.volume AS REAL) / NULLIF(avg.avg_vol, 0) AS vol_ratio
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (
                SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                WHERE trade_date < :td GROUP BY ts_code
            ) avg ON avg.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND d.ts_code NOT LIKE '%ST%'
              AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%'
              AND d.ts_code NOT LIKE '920%'
              AND d.pct_chg BETWEEN 0.5 AND 6
              AND d.volume > 0
              AND d.close > d.open
              AND CAST(d.volume AS REAL) / avg.avg_vol BETWEEN 0.5 AND 3.0
            ORDER BY CAST(d.volume AS REAL) / avg.avg_vol ASC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "量价稳健", exclude)

    def _dedup_rows(self, rows, reason_prefix: str = "", exclude: set | None = None) -> list:
        result = []
        seen = set()
        exc = exclude or set()
        for row in rows:
            code = row[0]
            if code in seen or code in exc:
                continue
            seen.add(code)
            pct = round(float(row[3]), 2) if row[3] else 0
            reason = f"{reason_prefix}，变动{pct:.2f}%"
            result.append({
                "stock_code": code,
                "stock_name": row[1],
                "close": round(float(row[2]), 2) if row[2] is not None else None,
                "change_pct": pct,
                "volume_ratio": round(float(row[5]), 2) if len(row) > 5 and row[5] is not None else None,
                "inclusion_reason": reason,
            })
            if len(result) >= POOL_SIZE:
                break
        return result

    async def _persist_pool(self, pool_type: str, items: list, calc_date: str):
        """将选股池结果写入 stock_pool_results 表。"""
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
                        "md": json.dumps({"close": item.get("close"), "change_pct": item.get("change_pct"), "volume_ratio": item.get("volume_ratio")}),
                        "ir": item.get("inclusion_reason", ""),
                    })
                except Exception:
                    continue
            await session.commit()
