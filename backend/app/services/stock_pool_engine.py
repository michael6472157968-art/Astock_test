"""选股池计算引擎——4大策略离线批量计算，基于SQLite真实数据。

收盘后运行一次，结果写入内存缓存。每池10只，互不重复。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings

logger = logging.getLogger("stock_pool")
_settings = get_settings()

POOL_SIZE = 15


class StockPoolEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        if not trade_date:
            # 使用最新可用的交易日数据，而不是 hardcode date.today()-1
            from app.core.database import async_session as _sess
            from sqlalchemy import text as _text
            async with _sess() as _session:
                _r = await _session.execute(_text('SELECT MAX(trade_date) FROM stock_daily'))
                trade_date = _r.scalar() or (date.today() - timedelta(days=1)).strftime('%Y%m%d')

        td5 = (date.today() - timedelta(days=8)).strftime("%Y%m%d")
        start20 = (date.today() - timedelta(days=25)).strftime("%Y%m%d")

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

        counts = {k: len(v) for k, v in pools.items()}
        logger.info(f"Stock pools computed for {trade_date}: {counts}")
        return pools

    async def _hot_leader(self, session, trade_date: str) -> list:
        """热点龙头池：放量突破 + 涨幅>3%，按涨幅降序取PoolSize只，同股去重"""
        sql = text("""
            SELECT DISTINCT d.ts_code, s.name, d.close, d.pct_chg, d.volume
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND d.pct_chg > 3
              AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%'
              AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%'
              AND d.ts_code NOT LIKE '920%'
              AND d.ts_code NOT LIKE '300%'
              AND d.ts_code NOT LIKE '301%'
            ORDER BY d.pct_chg DESC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, f"放量突破，涨幅")

    async def _dip_ambush(self, session, trade_date: str, start20: str, exclude: set) -> list:
        """低吸埋伏池：靠近20日低点+止跌缩量，与非ST，取10只"""
        sql = text("""
            SELECT DISTINCT d.ts_code, s.name, d.close, d.pct_chg, d.volume, low20.min_low
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
              AND d.ts_code NOT LIKE '300%'
              AND d.ts_code NOT LIKE '301%'
            ORDER BY d.volume ASC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "start20": start20, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "回调企稳", exclude)

    async def _oversold_rebound(self, session, trade_date: str, td5: str, exclude: set) -> list:
        """超跌反弹池：5日跌幅>5%，今日止跌，取15只"""
        sql = text("""
            SELECT DISTINCT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
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
              AND d.ts_code NOT LIKE '300%'
              AND d.ts_code NOT LIKE '301%'
              AND (d.close - d5.close) / NULLIF(d5.close, 0) * 100 < -5
              AND d.pct_chg > -4
            ORDER BY (d.close - d5.close) / NULLIF(d5.close, 0) * 100 ASC
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td5": td5, "lim": POOL_SIZE * 3})
        return self._dedup_rows(r, "超跌反弹", exclude)

    async def _steady_swing(self, session, trade_date: str, exclude: set) -> list:
        """稳健波段池：量价健康+涨幅适中+上涨，取10只"""
        sql = text("""
            SELECT DISTINCT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
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
              AND d.ts_code NOT LIKE '300%'
              AND d.ts_code NOT LIKE '301%'
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
                "close": round(float(row[2]), 2) if row[2] else None,
                "change_pct": pct,
                "volume_ratio": round(float(row[5]), 2) if len(row) > 5 and row[5] else None,
                "inclusion_reason": reason,
            })
            if len(result) >= POOL_SIZE:
                break
        return result
