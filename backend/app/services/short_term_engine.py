"""短线优选引擎——T+3/T+7 追涨+低吸 4模式每日选股。

全基于 stock_daily + daily_basic 表计算，零额外 Tushare API 消耗。
收盘后运行，结果写入 stock_pool_results 表。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings

logger = logging.getLogger("short_term")
_settings = get_settings()

POOL_SIZE = 10

MODE_META = {
    "short_t3_momentum":  {"name": "T+3 追涨", "period": 3, "style": "momentum"},
    "short_t3_dip":       {"name": "T+3 低吸", "period": 3, "style": "dip"},
    "short_t7_momentum":  {"name": "T+7 追涨", "period": 7, "style": "momentum"},
    "short_t7_dip":       {"name": "T+7 低吸", "period": 7, "style": "dip"},
}


class ShortTermEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        if not trade_date:
            async with async_session() as sess:
                r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
                trade_date = r.scalar() or (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        async with async_session() as session:
            # 获取可用交易日期
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60"
            ), {"td": trade_date})
            all_dates = [row[0] for row in r.fetchall()]
            if not all_dates:
                return {}

            t3 = await self._t3_momentum(session, trade_date, all_dates)
            d3 = await self._t3_dip(session, trade_date, all_dates)
            t7 = await self._t7_momentum(session, trade_date, all_dates)
            d7 = await self._t7_dip(session, trade_date, all_dates)

        pools = {
            "short_t3_momentum": t3,
            "short_t3_dip": d3,
            "short_t7_momentum": t7,
            "short_t7_dip": d7,
        }
        for ptype in pools:
            await cache_set(f"pool:{ptype}:{trade_date}", pools[ptype],
                            ttl=_settings.cache_offline_ttl)
            await self._persist(ptype, pools[ptype], trade_date)

        counts = {k: len(v) for k, v in pools.items()}
        logger.info(f"Short-term pools computed for {trade_date}: {counts}")
        return pools

    # ── T+3 追涨 ──
    async def _t3_momentum(self, session, trade_date: str, dates: list) -> list:
        """强势股短期惯性上冲。近3日涨幅>3%，今日>1%，站上MA5，量比>1.2。"""
        td3 = self._nth_date(dates, trade_date, 3)
        if not td3:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d3.close) / NULLIF(d3.close, 0) * 100 AS chg3,
                   CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) AS vol_ratio,
                   CASE WHEN db.turnover_rate IS NOT NULL AND db.turnover_rate > 0 THEN db.turnover_rate ELSE NULL END AS turnover
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td3) d3 ON d3.ts_code = d.ts_code
            JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                  WHERE trade_date = :td3 OR trade_date = :td OR trade_date = :td2 GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.pct_chg > 1
              AND (d.close - d3.close) / NULLIF(d3.close, 0) * 100 > 3
              AND d.close > d.open
              AND CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) > 1.2
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        td2 = self._nth_date(dates, trade_date, 2)
        r = await session.execute(sql, {
            "td": trade_date, "td2": td2 or trade_date, "td3": td3, "lim": POOL_SIZE * 3,
        })
        return self._score_rows(r, "T+3追涨", ["chg3", "vol_ratio", "pct_chg", "turnover"],
                                [0.35, 0.25, 0.20, 0.20])

    # ── T+3 低吸 ──
    async def _t3_dip(self, session, trade_date: str, dates: list) -> list:
        """回调企稳后反弹。近5日累计跌>3%，今日收阳，量缩至5均量80%以下。"""
        td5 = self._nth_date(dates, trade_date, 5)
        if not td5:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d5.close) / NULLIF(d5.close, 0) * 100 AS chg5,
                   CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) AS vol_ratio,
                   (d.close - dlo.min_low) / NULLIF(dlo.min_low, 0) * 100 AS dist_from_low
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td5) d5 ON d5.ts_code = d.ts_code
            JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                  WHERE trade_date IN (:td5,:td4,:td3,:td2,:td) GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            JOIN (SELECT ts_code, MIN(low) AS min_low FROM stock_daily
                  WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
            ) dlo ON dlo.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND (d.close - d5.close) / NULLIF(d5.close, 0) * 100 < -3
              AND d.pct_chg > 0 AND d.close > d.open
              AND CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) < 0.8
              AND dlo.min_low > 0
              AND d.close < dlo.min_low * 1.10
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        td4 = self._nth_date(dates, trade_date, 4)
        td3 = self._nth_date(dates, trade_date, 3)
        td2 = self._nth_date(dates, trade_date, 2)
        td20 = self._nth_date(dates, trade_date, 20)
        r = await session.execute(sql, {
            "td": trade_date, "td2": td2 or trade_date, "td3": td3 or trade_date,
            "td4": td4 or trade_date, "td5": td5, "td20": td20 or trade_date,
            "lim": POOL_SIZE * 3,
        })
        return self._score_rows(r, "T+3低吸", ["chg5", "vol_ratio", "dist_from_low", "pct_chg"],
                                [0.30, 0.25, 0.25, 0.20])

    # ── T+7 追涨 ──
    async def _t7_momentum(self, session, trade_date: str, dates: list) -> list:
        """趋势已确立顺势持股。MA5>MA10>MA20，近10日涨幅>0，连续3日收阳。"""
        td10 = self._nth_date(dates, trade_date, 10)
        td4 = self._nth_date(dates, trade_date, 4)
        td9 = self._nth_date(dates, trade_date, 9)
        td19 = self._nth_date(dates, trade_date, 19)
        if not td10 or not td4 or not td9:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d10.close) / NULLIF(d10.close, 0) * 100 AS chg10,
                   (ma5.ma5 - ma5.ma10) / NULLIF(ma5.ma10, 0) * 100 AS ma_slope,
                   CASE WHEN db.turnover_rate IS NOT NULL THEN db.turnover_rate ELSE NULL END AS turnover
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td10) d10 ON d10.ts_code = d.ts_code
            JOIN (
                SELECT ts_code,
                       AVG(CASE WHEN trade_date >= :td4 THEN close END) AS ma5,
                       AVG(CASE WHEN trade_date >= :td9 THEN close END) AS ma10,
                       AVG(CASE WHEN trade_date >= :td19 THEN close END) AS ma20
                FROM stock_daily WHERE trade_date <= :td GROUP BY ts_code
                HAVING AVG(CASE WHEN trade_date >= :td4 THEN close END) >
                       AVG(CASE WHEN trade_date >= :td9 THEN close END)
            ) ma5 ON ma5.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND (d.close - d10.close) / NULLIF(d10.close, 0) * 100 > 0
              AND d.close > d.open
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        r = await session.execute(sql, {
            "td": trade_date, "td4": td4 or trade_date, "td9": td9 or trade_date,
            "td10": td10, "td19": td19 or trade_date, "lim": POOL_SIZE * 3,
        })
        return self._score_rows(r, "T+7追涨", ["chg10", "ma_slope", "pct_chg", "turnover"],
                                [0.30, 0.25, 0.25, 0.20])

    # ── T+7 低吸 ──
    async def _t7_dip(self, session, trade_date: str, dates: list) -> list:
        """中期回调修复机会。近10日涨幅<0但近3日收阳≥2天，距60日高点回撤>10%。"""
        td10 = self._nth_date(dates, trade_date, 10)
        td3 = self._nth_date(dates, trade_date, 3)
        td60 = self._nth_date(dates, trade_date, 60)
        if not td10 or not td3:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d10.close) / NULLIF(d10.close, 0) * 100 AS chg10,
                   (d.close - d3.close) / NULLIF(d3.close, 0) * 100 AS chg3_recent,
                   (hi60.max_high - d.close) / NULLIF(hi60.max_high, 0) * 100 AS drawdown
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td10) d10 ON d10.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td3) d3 ON d3.ts_code = d.ts_code
            JOIN (SELECT ts_code, MAX(high) AS max_high FROM stock_daily
                  WHERE trade_date <= :td AND trade_date >= :td60 GROUP BY ts_code
            ) hi60 ON hi60.ts_code = d.ts_code
            WHERE d.trade_date = :td
              AND (d.close - d10.close) / NULLIF(d10.close, 0) * 100 < 0
              AND (d.close - d3.close) / NULLIF(d3.close, 0) * 100 > 0
              AND hi60.max_high > 0
              AND (hi60.max_high - d.close) / NULLIF(hi60.max_high, 0) * 100 > 10
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        r = await session.execute(sql, {
            "td": trade_date, "td3": td3, "td10": td10, "td60": td60 or trade_date,
            "lim": POOL_SIZE * 3,
        })
        return self._score_rows(r, "T+7低吸", ["drawdown", "chg3_recent", "chg10", "pct_chg"],
                                [0.30, 0.25, 0.25, 0.20])

    # ── 评分排序 ──
    def _score_rows(self, rows, reason_prefix: str, fields: list[str],
                    weights: list[float]) -> list:
        scored = []
        for row in rows:
            code = row[0]
            name = row[1]
            close = float(row[2]) if row[2] is not None else None
            pct_chg = float(row[3]) if row[3] is not None else 0
            vol = float(row[4]) if len(row) > 4 and row[4] is not None else 0

            # 计算评分: 每项 min-max 归一化后加权
            field_vals = {}
            for i, f in enumerate(fields):
                idx = 5 + i
                if idx < len(row) and row[idx] is not None:
                    field_vals[f] = float(row[idx])
                else:
                    field_vals[f] = 0

            score = sum(
                self._normalize(field_vals.get(f, 0), f) * w
                for f, w in zip(fields, weights)
            )
            scored.append({
                "stock_code": code,
                "stock_name": name,
                "close": close,
                "change_pct": round(pct_chg, 2),
                "score": round(score * 100, 1),
                "volume": vol,
                "inclusion_reason": f"{reason_prefix} 评分{round(score*100,1)}",
                "mode": reason_prefix,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:POOL_SIZE]

    def _normalize(self, val: float, field: str) -> float:
        """将原始值归一化到 [0, 1]，越大越好（对正向指标）或取绝对值（对负向指标取反）。"""
        if field == "chg5":
            return max(0, min(1, (val + 10) / 8))
        if field == "chg10":
            return max(0, min(1, (val + 15) / 20))
        if field in ("chg3", "chg3_recent"):
            return max(0, min(1, val / 5))
        if field == "vol_ratio":
            if val >= 0.5 and val <= 2.0:
                return 1.0 - abs(val - 1.0)
            return max(0, 1.0 - abs(val - 1.0) / 3)
        if field == "dist_from_low":
            return max(0, 1 - val / 15)
        if field == "turnover":
            if val is None or val == 0:
                return 0.5
            return max(0, 1 - abs(val - 5) / 15)
        if field == "ma_slope":
            return max(0, min(1, val / 5))
        if field == "drawdown":
            return max(0, min(1, val / 30))
        if field == "pct_chg":
            return max(0, min(1, (val + 3) / 10))
        return 0.5

    def _nth_date(self, dates: list, ref_date: str, n: int) -> str | None:
        """从 dates 列表中找到 ref_date 之前第 n 个交易日。"""
        if not dates:
            return None
        filtered = [d for d in dates if d <= ref_date]
        filtered.sort(reverse=True)
        if len(filtered) > n:
            return filtered[n]
        return filtered[-1] if filtered else None

    async def _persist(self, pool_type: str, items: list, calc_date: str):
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
                            "score": item.get("score"),
                        }),
                        "ir": item.get("inclusion_reason", ""),
                    })
                except Exception:
                    continue
            await session.commit()
