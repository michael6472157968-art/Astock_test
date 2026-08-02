"""板块轮动分析引擎——基于行业分类统计涨跌幅排行。"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

from app.core.cache import cache_get, cache_set
from app.core.database import async_session
from app.core.settings import get_settings

logger = logging.getLogger("sector")
_settings = get_settings()


class SectorAnalysisEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        if not trade_date:
            trade_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        td5 = (date.today() - timedelta(days=6)).strftime("%Y%m%d")

        async with async_session() as session:
            r = await session.execute(text("""
                SELECT s.industry,
                       AVG(d.pct_chg) as avg_pct,
                       COUNT(*) as cnt,
                       MAX(d.pct_chg) as max_pct,
                       MIN(d.pct_chg) as min_pct
                FROM stock_daily d
                JOIN stocks s ON s.ts_code = d.ts_code
                WHERE d.trade_date = :td AND s.industry != ''
                GROUP BY s.industry
                HAVING COUNT(*) >= 5
                ORDER BY avg_pct DESC
                LIMIT 50
            """), {"td": trade_date})

            rows = list(r)

            # 批量查询所有行业的5日平均值（消除 N+1）
            prev_map: dict[str, float] = {}
            if rows:
                industries = [row[0] for row in rows]
                placeholders = ",".join([f":ind{i}" for i in range(len(industries))])
                params = {"td5": td5}
                for i, ind in enumerate(industries):
                    params[f"ind{i}"] = ind
                prev_r = await session.execute(text(f"""
                    SELECT s2.industry, AVG(d2.pct_chg)
                    FROM stock_daily d2
                    JOIN stocks s2 ON s2.ts_code = d2.ts_code
                    WHERE d2.trade_date = :td5 AND s2.industry IN ({placeholders})
                    GROUP BY s2.industry
                """), params)
                for row in prev_r:
                    v = row[1]
                    prev_map[row[0]] = round(float(v), 2) if v else 0

            sectors = []
            for row in rows:
                industry = row[0]
                avg_pct = round(float(row[1]), 2)
                cnt = row[2]
                prev_avg = prev_map.get(industry, 0)

                momentum = avg_pct - prev_avg
                heat = round(avg_pct + momentum * 2, 2)

                if avg_pct > 2 and momentum > 1:
                    phase = "加速上涨"
                elif avg_pct > 0 and momentum > 0:
                    phase = "持续走强"
                elif avg_pct > 0 and momentum <= 0:
                    phase = "高位分化"
                elif avg_pct < -2:
                    phase = "加速下跌"
                elif avg_pct < 0:
                    phase = "弱势回调"
                else:
                    phase = "震荡整理"

                sectors.append({
                    "name": industry,
                    "avg_pct": avg_pct,
                    "count": cnt,
                    "max_pct": round(float(row[3]), 2),
                    "min_pct": round(float(row[4]), 2),
                    "heat_score": heat,
                    "momentum": round(momentum, 2),
                    "prev_5d_avg": prev_avg,
                    "phase": phase,
                })

        await cache_set(f"sector:ranking:{trade_date}", sectors, ttl=_settings.cache_offline_ttl)
        logger.info(f"Sector analysis computed for {trade_date}: {len(sectors)} industries")
        return sectors
