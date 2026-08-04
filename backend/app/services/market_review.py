"""每日复盘简报引擎——基于日线数据自动生成大盘概览。"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import text

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings

logger = logging.getLogger("review")
_settings = get_settings()


class MarketReviewEngine:

    async def compute(self, trade_date: str = "") -> dict:
        if not trade_date:
            trade_date = await self._latest_date()
        if not trade_date:
            return {"date": "", "content": {"summary": "暂无日线数据"}}

        async with async_session() as session:
            r = await session.execute(text("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END),
                    AVG(pct_chg),
                    MAX(pct_chg),
                    MIN(pct_chg)
                FROM stock_daily WHERE trade_date = :td
            """), {"td": trade_date})
            row = r.fetchone()

            limit_up = 0
            limit_down = 0
            r_lim = await session.execute(text("""
                SELECT COUNT(*) FILTER (WHERE "limit" = 'U'),
                       COUNT(*) FILTER (WHERE "limit" = 'D')
                FROM limit_list_records WHERE trade_date = :td
            """), {"td": trade_date})
            lr = r_lim.first()
            if lr and (lr[0] or lr[1]):
                limit_up = int(lr[0] or 0)
                limit_down = int(lr[1] or 0)
            else:
                # 降级：表空时用日线 pct_chg 估算
                r_fb = await session.execute(text("""
                    SELECT COUNT(*) FILTER (WHERE pct_chg >= 9.8) as up,
                           COUNT(*) FILTER (WHERE pct_chg <= -9.8) as down
                    FROM stock_daily WHERE trade_date = :td
                """), {"td": trade_date})
                fb_row = r_fb.first()
                if fb_row:
                    limit_up = int(fb_row[0] or 0)
                    limit_down = int(fb_row[1] or 0)

            r2 = await session.execute(text("""
                SELECT DISTINCT s.name, d.pct_chg, s.industry
                FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code
                WHERE d.trade_date = :td AND d.pct_chg > 0
                ORDER BY d.pct_chg DESC LIMIT 5
            """), {"td": trade_date})
            top_gainers = [{"name": x[0], "pct": round(float(x[1]), 2), "industry": x[2]} for x in r2]

            r3 = await session.execute(text("""
                SELECT DISTINCT s.name, d.pct_chg, s.industry
                FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code
                WHERE d.trade_date = :td AND d.pct_chg < 0
                ORDER BY d.pct_chg ASC LIMIT 5
            """), {"td": trade_date})
            top_losers = [{"name": x[0], "pct": round(float(x[1]), 2), "industry": x[2]} for x in r3]

            r4 = await session.execute(text("""
                SELECT s.industry, AVG(d.pct_chg)
                FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code
                WHERE d.trade_date = :td AND s.industry != ''
                GROUP BY s.industry HAVING COUNT(*) >= 5
                ORDER BY AVG(d.pct_chg) DESC LIMIT 5
            """), {"td": trade_date})
            top_sectors = [{"name": x[0], "avg_pct": round(float(x[1]), 2)} for x in r4]

        total = int(row[0] or 0)
        up_count = int(row[1] or 0)
        flat_count = int(row[2] or 0); down_count = int(row[3] or 0)
        up_ratio = round(up_count / total * 100, 1) if total else 0

        content = {
            "total": total,
            "up_count": up_count, "down_count": down_count, "flat_count": flat_count,
            "up_ratio": up_ratio,
            "limit_up": limit_up, "limit_down": limit_down,
            "avg_pct": round(float(row[4] or 0), 2),
            "max_pct": round(float(row[5] or 0), 2),
            "min_pct": round(float(row[6] or 0), 2),
            "summary": f"全市场{total}只，涨{up_count}平{flat_count}跌{down_count}({up_ratio}%↑)，涨停{limit_up}只跌停{limit_down}只，均涨{round(float(row[4] or 0),2)}%",
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "top_sectors": top_sectors,
        }

        review_data = {"date": trade_date, "content": content}
        await cache_set(f"review:{trade_date}", review_data, ttl=_settings.cache_offline_ttl)
        logger.info(f"Market review computed for {trade_date}")
        return review_data

    async def _latest_date(self) -> str:
        async with async_session() as s:
            r = await s.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
            return r.scalar()
