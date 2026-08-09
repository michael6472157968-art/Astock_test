"""短线优选引擎——T+3/T+7 追涨+低吸 4模式每日选股。

全基于 stock_daily + daily_basic 表计算，零额外 Tushare API 消耗。
收盘后运行，结果写入 stock_pool_results 表。
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

logger = logging.getLogger("short_term")
_settings = get_settings()

POOL_SIZE = 10
_WEIGHTS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "factor_weights.json"

MODE_META = {
    "short_t3_momentum":  {"name": "T+3 追涨", "period": 3, "style": "momentum"},
    "short_t3_dip":       {"name": "T+3 低吸", "period": 3, "style": "dip"},
    "short_t7_momentum":  {"name": "T+7 追涨", "period": 7, "style": "momentum"},
    "short_t7_dip":       {"name": "T+7 低吸", "period": 7, "style": "dip"},
}


class ShortTermEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        # 加载因子权重配置（每次计算时重新读取，支持热更新）
        with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            fw = json.load(f)

        if not trade_date:
            async with async_session() as sess:
                r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
                trade_date = r.scalar() or (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        async with async_session() as session:
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60"
            ), {"td": trade_date})
            all_dates = [row[0] for row in r.fetchall()]
            if not all_dates:
                return {}

            # ── 去重优先级链：T+3追涨 > T+3低吸 > T+7追涨 > T+7低吸 ──
            # 超卖例外：dist_from_low<=10 或 drawdown>=20 可跨池保留

            t3m = await self._t3_momentum(session, trade_date, all_dates, fw)
            excluded = {item["stock_code"] for item in t3m}

            # T+3低吸：排除已在T+3追涨中的股票（超卖除外）
            t3d_candidates = await self._t3_dip_raw(session, trade_date, all_dates, fw)
            t3d = self._dedup_pool(t3d_candidates, excluded, "dist_from_low", 10)
            excluded = excluded | {item["stock_code"] for item in t3d}

            # T+7追涨：排除已在T+3两池中的股票（无超卖豁免，momentum风格无超卖字段）
            t7m_candidates = await self._t7_momentum_raw(session, trade_date, all_dates, fw)
            t7m = self._dedup_pool(t7m_candidates, excluded, None, None)
            excluded = excluded | {item["stock_code"] for item in t7m}

            # T+7低吸：排除所有高优先级池中的股票（超卖除外）
            t7d_candidates = await self._t7_dip_raw(session, trade_date, all_dates, fw)
            t7d = self._dedup_pool(t7d_candidates, excluded, "drawdown", 20, "gte")

        pools = {
            "short_t3_momentum": t3m,
            "short_t3_dip": t3d,
            "short_t7_momentum": t7m,
            "short_t7_dip": t7d,
        }
        for ptype in pools:
            # 剥离内部字段再对外暴露（缓存 + DB）
            cleaned = [{k: v for k, v in item.items() if not k.startswith("_")} for item in pools[ptype]]
            await cache_set(f"pool:{ptype}:{trade_date}", cleaned,
                            ttl=_settings.cache_offline_ttl)
            await self._persist(ptype, cleaned, trade_date)

        counts = {k: len(v) for k, v in pools.items()}
        logger.info(f"Short-term pools computed for {trade_date}: {counts}")
        return pools

    def _dedup_pool(self, candidates: list, excluded: set, oversold_field: str | None,
                    oversold_threshold: float | None,
                    oversold_direction: str = "lte") -> list:
        """从候选中排除已在高优先级池中的股票，超卖信号可豁免，不足时从被排除项补位。

        oversold_direction: "lte" (<=) 用于 dist_from_low（越小越超卖），
                           "gte" (>=) 用于 drawdown（越大越超卖）。"""
        result = []
        skipped = []
        for item in candidates:
            code = item["stock_code"]
            if code in excluded:
                if oversold_field:
                    val = item.get("_raw_oversold_val", 0)
                    if val is not None:
                        if oversold_direction == "gte":
                            if val >= oversold_threshold:
                                result.append(item)
                                continue
                        elif val <= oversold_threshold:
                            result.append(item)
                            continue
                skipped.append(item)
            else:
                result.append(item)
            if len(result) >= POOL_SIZE:
                break

        # 补位：结果不足POOL_SIZE时从被排除的候选中填充
        if len(result) < POOL_SIZE:
            for item in skipped:
                if len(result) >= POOL_SIZE:
                    break
                if item not in result:
                    result.append(item)

        return result

    # ── T+3 追涨 ──
    async def _t3_momentum(self, session, trade_date: str, dates: list, fw: dict) -> list:
        items = await self._t3_momentum_raw(session, trade_date, dates, fw)
        return items[:POOL_SIZE]

    async def _t3_momentum_raw(self, session, trade_date: str, dates: list, fw: dict):
        td3 = self._nth_date(dates, trade_date, 3)
        if not td3:
            return []

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
        return score_and_rank(r.fetchall(), fw["short_t3_momentum"],
                              "T+3追涨", limit=POOL_SIZE * 3)

    # ── T+3 低吸 ──
    async def _t3_dip(self, session, trade_date: str, dates: list, fw: dict) -> list:
        items = await self._t3_dip_raw(session, trade_date, dates, fw)
        return items[:POOL_SIZE]

    async def _t3_dip_raw(self, session, trade_date: str, dates: list, fw: dict):
        td5 = self._nth_date(dates, trade_date, 5)
        if not td5:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d5.close) / NULLIF(d5.close, 0) * 100 AS chg5,
                   CAST(d.volume AS REAL) / NULLIF(av.avg_vol, 0) AS vol_ratio,
                   (d.close - dlo.min_low) / NULLIF(dlo.min_low, 0) * 100 AS dist_from_low,
                   CASE WHEN db.turnover_rate IS NOT NULL THEN db.turnover_rate ELSE NULL END AS turnover,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td5) d5 ON d5.ts_code = d.ts_code
            JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
                  WHERE trade_date IN (:td5,:td4,:td3,:td2,:td) GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            JOIN (SELECT ts_code, MIN(low) AS min_low FROM stock_daily
                  WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
            ) dlo ON dlo.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
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
        return score_and_rank(r.fetchall(), fw["short_t3_dip"],
                              "T+3低吸", limit=POOL_SIZE * 3)

    # ── T+7 追涨 ──
    async def _t7_momentum(self, session, trade_date: str, dates: list, fw: dict) -> list:
        items = await self._t7_momentum_raw(session, trade_date, dates, fw)
        return items[:POOL_SIZE]

    async def _t7_momentum_raw(self, session, trade_date: str, dates: list, fw: dict):
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
                   CASE WHEN db.turnover_rate IS NOT NULL THEN db.turnover_rate ELSE NULL END AS turnover,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
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
        return score_and_rank(r.fetchall(), fw["short_t7_momentum"],
                              "T+7追涨", limit=POOL_SIZE * 3)

    # ── T+7 低吸 ──
    async def _t7_dip(self, session, trade_date: str, dates: list, fw: dict) -> list:
        items = await self._t7_dip_raw(session, trade_date, dates, fw)
        return items[:POOL_SIZE]

    async def _t7_dip_raw(self, session, trade_date: str, dates: list, fw: dict):
        td10 = self._nth_date(dates, trade_date, 10)
        td3 = self._nth_date(dates, trade_date, 3)
        td60 = self._nth_date(dates, trade_date, 60)
        if not td10 or not td3:
            return []

        sql = text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d10.close) / NULLIF(d10.close, 0) * 100 AS chg10,
                   (d.close - d3.close) / NULLIF(d3.close, 0) * 100 AS chg3_recent,
                   (hi60.max_high - d.close) / NULLIF(hi60.max_high, 0) * 100 AS drawdown,
                   CASE WHEN db.turnover_rate IS NOT NULL THEN db.turnover_rate ELSE NULL END AS turnover,
                   CASE WHEN db.pe IS NOT NULL AND db.pe > 0 THEN db.pe ELSE NULL END AS pe,
                   CASE WHEN db.pb IS NOT NULL AND db.pb > 0 THEN db.pb ELSE NULL END AS pb,
                   CASE WHEN db.total_mv IS NOT NULL AND db.total_mv > 0 THEN db.total_mv ELSE NULL END AS total_mv
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td10) d10 ON d10.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td3) d3 ON d3.ts_code = d.ts_code
            JOIN (SELECT ts_code, MAX(high) AS max_high FROM stock_daily
                  WHERE trade_date <= :td AND trade_date >= :td60 GROUP BY ts_code
            ) hi60 ON hi60.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
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
        return score_and_rank(r.fetchall(), fw["short_t7_dip"],
                              "T+7低吸", limit=POOL_SIZE * 3)

    # ── 辅助 ──

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
