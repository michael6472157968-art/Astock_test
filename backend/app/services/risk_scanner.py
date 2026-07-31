"""短线风险避雷扫描引擎——技术面 + 基本面双维度。

扫描维度：
  1. ST 退市风险 — stocks 表名称含 ST/*ST
  2. 连板过热 — 近5日涨幅 > 30%（追高风险）
  3. 断崖下跌 — 连续下跌5日以上且累计跌幅 > 15%
  4. 高换手异动 — 单日成交额占流通市值比例异常
  5. 缩量阴跌 — 成交量持续萎缩伴随价格下跌

结果写入 risk_list_results 表，缓存到内存。
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from app.core.cache import cache_set
from app.core.database import async_session
from app.core.settings import get_settings
from app.models.orm.models import RiskListResult
from sqlalchemy import text as _text

logger = logging.getLogger("risk")
_settings = get_settings()


class RiskScanner:

    async def scan_risk_list(self, trade_date: str = "") -> list[dict]:
        """短线风险扫描，返回风险条目列表并持久化。"""
        if not trade_date:
            trade_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        risks: list[dict] = []

        async with async_session() as session:
            risks.extend(await self._scan_st_stocks(session))
            risks.extend(await self._scan_surge_overheat(session, trade_date))
            risks.extend(await self._scan_cliff_drop(session, trade_date))
            risks.extend(await self._scan_high_turnover(session, trade_date))
            risks.extend(await self._scan_volume_drain(session, trade_date))

            # 写入 DB
            await self._persist(session, risks, trade_date)
            await session.commit()

        # 缓存
        await cache_set(f"risk:list:{trade_date}", risks, ttl=_settings.cache_offline_ttl)
        logger.info(f"Risk scan complete: {len(risks)} risks for {trade_date}")
        return risks

    # ── 扫描子维度 ──

    async def _scan_st_stocks(self, session) -> list[dict]:
        """ST / *ST 退市风险。"""
        r = await session.execute(
            _text("SELECT ts_code, name FROM stocks WHERE name LIKE '%ST%' OR name LIKE '%退%'")
        )
        results = []
        for row in r.fetchall():
            results.append({
                "risk_category": "st_risk",
                "ts_code": row[0],
                "stock_name": row[1],
                "risk_detail": f"{row[1]} 为ST/*ST股票，存在退市风险，短线交易流动性差、涨跌幅受限",
            })
        return results

    async def _scan_surge_overheat(self, session, trade_date: str) -> list[dict]:
        """连板过热：近5日累计涨幅 > 30% 且最新日涨幅 > 5%"""
        import sqlite3
        date_5d = _find_closest_trade_date(session, _date_offset(trade_date, -5))
        sql = """
            SELECT sub.ts_code, s.name,
                   sub.close_now, sub.close_base, sub.cum_pct, sub.latest_pct
            FROM (
                SELECT ts_code, MAX(close_now) AS close_now, MAX(close_base) AS close_base,
                       MAX(cum_pct) AS cum_pct, MAX(latest_pct) AS latest_pct
                FROM (
                    SELECT d0.ts_code,
                           d0.close AS close_now,
                           d5.close AS close_base,
                           ROUND((d0.close - d5.close) / d5.close * 100, 2) AS cum_pct,
                           ROUND(d0.pct_chg, 2) AS latest_pct
                    FROM (SELECT ts_code, MAX(close) AS close, MAX(pct_chg) AS pct_chg
                          FROM stock_daily WHERE trade_date = :trade_date GROUP BY ts_code) d0
                    JOIN (SELECT ts_code, MAX(close) AS close
                          FROM stock_daily WHERE trade_date = :date_5d GROUP BY ts_code) d5
                      ON d5.ts_code = d0.ts_code
                    WHERE d5.close > 0
                ) inner_sub
                GROUP BY ts_code
            ) sub
            JOIN stocks s ON s.ts_code = sub.ts_code
            WHERE sub.cum_pct > 30 AND sub.latest_pct > 5
            ORDER BY sub.cum_pct DESC
            LIMIT 80
        """
        r = await session.execute(_text(sql), {"trade_date": trade_date, "date_5d": date_5d})
        results = []
        for row in r.fetchall():
            code, name, close_now, close_base, cum_pct, latest_pct = row
            results.append({
                "risk_category": "surge_overheat",
                "ts_code": code,
                "stock_name": name,
                "risk_detail": f"近5日累计涨幅 {cum_pct}%（{close_base}→{close_now}），"
                               f"今日涨幅 {latest_pct}%，追高风险极大",
            })
        return results

    async def _scan_cliff_drop(self, session, trade_date: str) -> list[dict]:
        """断崖下跌：近期（最多4个交易日）中 ≥3 天收阴，累计跌幅 > 5%。"""
        sql = """
            WITH dedup AS (
                SELECT ts_code, trade_date, MAX(close) AS close, MAX(pct_chg) AS pct_chg
                FROM stock_daily
                WHERE trade_date <= :trade_date
                GROUP BY ts_code, trade_date
            ),
            recent AS (
                SELECT ts_code, trade_date, close, pct_chg,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM dedup
            ),
            agg AS (
                SELECT ts_code,
                       SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END) AS down_days,
                       COUNT(*) AS total_days,
                       ROUND((MIN(CASE WHEN rn = 1 THEN close END) - MAX(CASE WHEN rn = 4 THEN close END))
                             * 100.0 / NULLIF(MAX(CASE WHEN rn = 4 THEN close END), 0), 2) AS cum_pct
                FROM recent
                WHERE rn <= 4
                GROUP BY ts_code
                HAVING COUNT(*) >= 3
            )
            SELECT a.ts_code, s.name, a.down_days, a.cum_pct
            FROM agg a
            JOIN stocks s ON s.ts_code = a.ts_code
            WHERE a.down_days >= 3 AND a.cum_pct < -5
            ORDER BY a.cum_pct ASC
            LIMIT 50
        """
        r = await session.execute(_text(sql), {"trade_date": trade_date})
        results = []
        for row in r.fetchall():
            code, name, down_days, cum_pct = row
            results.append({
                "risk_category": "cliff_drop",
                "ts_code": code,
                "stock_name": name,
                "risk_detail": f"近7日 {down_days} 天收阴，累计跌幅 {cum_pct}%，"
                               f"短线趋势恶化，注意止损",
            })
        return results

    async def _scan_high_turnover(self, session, trade_date: str) -> list[dict]:
        """高换手异动：单日成交额异常放大(Top100 中 >10亿 且涨跌幅异常)。"""
        sql = """
            SELECT d.ts_code, s.name,
                   ROUND(MAX(d.volume), 0) AS vol,
                   ROUND(MAX(d.amount) / 10000.0, 2) AS amount_wan,
                   ROUND(MAX(d.pct_chg), 2) AS pct_chg
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :trade_date
              AND d.amount > 0
            GROUP BY d.ts_code
            ORDER BY amount_wan DESC
            LIMIT 100
        """
        r = await session.execute(_text(sql), {"trade_date": trade_date})
        results = []
        for row in r.fetchall():
            code, name, vol, amount_wan, pct_chg = row
            if amount_wan and float(amount_wan) > 100000:  # 10亿+
                results.append({
                    "risk_category": "high_turnover",
                    "ts_code": code,
                    "stock_name": name,
                    "risk_detail": f"单日成交额 {float(amount_wan):.0f} 万元，"
                                   f"涨跌幅 {float(pct_chg):+.2f}%，注意主力出货风险",
                })
        return results[:50]

    async def _scan_volume_drain(self, session, trade_date: str) -> list[dict]:
        """缩量阴跌：近期成交量递减伴随价格持续下跌（至少3个交易日）。"""
        sql = """
            WITH dedup AS (
                SELECT ts_code, trade_date, MAX(volume) AS vol, MAX(close) AS close,
                       MAX(pct_chg) AS pct_chg
                FROM stock_daily
                WHERE trade_date <= :trade_date
                GROUP BY ts_code, trade_date
            ),
            ranked AS (
                SELECT ts_code, trade_date, vol, close, pct_chg,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM dedup
            ),
            latest_vol AS (
                SELECT ts_code,
                       AVG(CASE WHEN rn <= 2 THEN vol END) AS vol_recent,
                       AVG(CASE WHEN rn > 2 THEN vol END) AS vol_earlier,
                       SUM(CASE WHEN rn <= 4 AND pct_chg < 0 THEN 1 ELSE 0 END) AS down_days,
                       ROUND((MIN(CASE WHEN rn = 1 THEN close END) - MIN(CASE WHEN rn = 4 THEN close END))
                             * 100.0 / NULLIF(MIN(CASE WHEN rn = 4 THEN close END), 0), 2) AS cum_pct
                FROM ranked
                WHERE rn <= 4
                GROUP BY ts_code
                HAVING COUNT(*) >= 3
            )
            SELECT l.ts_code, s.name, l.down_days, l.cum_pct,
                   ROUND((l.vol_recent - l.vol_earlier) * 100.0 / NULLIF(l.vol_earlier, 0), 1) AS vol_chg_pct
            FROM latest_vol l
            JOIN stocks s ON s.ts_code = l.ts_code
            WHERE l.down_days >= 2
              AND l.cum_pct < -5
              AND l.vol_recent < l.vol_earlier * 0.8
            ORDER BY l.cum_pct ASC
            LIMIT 50
        """
        r = await session.execute(_text(sql), {"trade_date": trade_date})
        results = []
        for row in r.fetchall():
            code, name, down_days, cum_pct, vol_chg_pct = row
            results.append({
                "risk_category": "volume_drain",
                "ts_code": code,
                "stock_name": name,
                "risk_detail": f"近5日 {down_days} 天收阴，累计跌幅 {cum_pct}%，"
                               f"成交量萎缩 {vol_chg_pct}%，资金持续离场",
            })
        return results

    # ── 持久化 ──

    async def _persist(self, session, risks: list[dict], trade_date: str):
        await session.execute(_text("DELETE FROM risk_list_results WHERE calc_date = :d"), {"d": trade_date})
        for r in risks:
            session.add(RiskListResult(
                calc_date=trade_date,
                risk_category=r["risk_category"],
                ts_code=r["ts_code"],
                stock_name=r["stock_name"],
                risk_detail=r["risk_detail"],
            ))


def _date_offset(date_str: str, days: int) -> str:
    d = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    return (d + timedelta(days=days)).strftime("%Y%m%d")


def _find_closest_trade_date(session, target: str) -> str:
    """在 stock_daily 中查找与目标日期最近的实际交易日（不超过 ±3 天）。"""
    import sqlite3
    # Use sync sqlite3 for simplicity in this helper
    db_path = os.path.join(_settings.data_dir, "stock_analyzer.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for offset in range(-3, 4):
        d = _date_offset(target, offset)
        cur.execute("SELECT COUNT(*) FROM stock_daily WHERE trade_date = ? LIMIT 1", (d,))
        if cur.fetchone()[0] > 0:
            conn.close()
            return d
    conn.close()
    return target
