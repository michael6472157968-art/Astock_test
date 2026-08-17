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

            dip = await self._dip_ambush(session, trade_date, start20, all_dates, used, fw)
            used.update(s["stock_code"] for s in dip if s["stock_code"])

            bounce = await self._oversold_rebound(session, trade_date, td5, all_dates, used, fw)
            used.update(s["stock_code"] for s in bounce if s["stock_code"])

            steady = await self._steady_swing(session, trade_date, all_dates, used, fw)

            factor_short = await self._factor_short(session, trade_date, all_dates, fw)
            factor_long = await self._factor_long(session, trade_date, all_dates, fw)

        pools = {
            "hot_leader": hot, "dip_ambush": dip,
            "oversold_rebound": bounce, "steady_swing": steady,
            "factor_short": factor_short,
            "factor_long": factor_long,
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
        td20 = self._nth_date(dates, trade_date, 20)
        if not td3 or not td20:
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
                  WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.pct_chg > 3
              AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td3": td3, "td20": td20, "lim": POOL_SIZE * 3})
        return score_and_rank(r.fetchall(), fw["hot_leader"], "热点龙头", limit=POOL_SIZE)

    # ── 低吸埋伏池 ──

    async def _dip_ambush(self, session, trade_date: str, start20: str, dates: list, exclude: set, fw: dict) -> list:
        td20 = self._nth_date(dates, trade_date, 20)
        if not td20:
            td20 = start20
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
                       WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
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
        r = await session.execute(sql, {"td": trade_date, "start20": start20, "td20": td20 or start20, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["dip_ambush"], "低吸埋伏", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 超跌反弹池 ──

    async def _oversold_rebound(self, session, trade_date: str, td5: str, dates: list, exclude: set, fw: dict) -> list:
        result = await self._try_oversold_rebound(session, trade_date, td5, dates, exclude, fw)
        if len(result) < 10:
            fallback_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=5)).strftime("%Y%m%d")
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :fb "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
            ), {"fb": fallback_date})
            fb_row = r.fetchone()
            if fb_row and str(fb_row[0]) != str(td5):
                fallback_result = await self._try_oversold_rebound(session, trade_date, str(fb_row[0]), dates, exclude, fw)
                if len(fallback_result) > len(result):
                    result = fallback_result
        return result

    async def _try_oversold_rebound(self, session, trade_date: str, td5: str, dates: list, exclude: set, fw: dict) -> list:
        td20 = self._nth_date(dates, trade_date, 20)
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
                       WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
            ) av ON av.ts_code = d.ts_code
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.trade_date = :td
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
              AND (d.close - d5.close) / NULLIF(d5.close, 0) * 100 < -5
              AND d.pct_chg > -4
            LIMIT :lim
        """)
        r = await session.execute(sql, {"td": trade_date, "td5": td5, "td20": td20 or trade_date, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["oversold_rebound"], "超跌反弹", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 稳健波段池 ──

    async def _steady_swing(self, session, trade_date: str, dates: list, exclude: set, fw: dict) -> list:
        td5 = self._nth_date(dates, trade_date, 5)
        td20 = self._nth_date(dates, trade_date, 20)
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
                WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code
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
        r = await session.execute(sql, {"td": trade_date, "td5": td5 or trade_date, "td20": td20 or trade_date, "lim": POOL_SIZE * 3})
        candidates = score_and_rank(r.fetchall(), fw["steady_swing"], "稳健波段", limit=POOL_SIZE * 3)
        return [c for c in candidates if c["stock_code"] not in exclude][:POOL_SIZE]

    # ── 因子选股池（短线20日 + 长线60日 分层）──

    @staticmethod
    def _corr_price_vol(pairs: list) -> float | None:
        """近N日 (close, volume) 序列的价量相关系数（量价背离：负相关=缩量涨，越负越健康）。"""
        n = len(pairs)
        if n < 10:
            return None
        closes = [p[0] for p in pairs]
        vols = [p[1] for p in pairs]
        mc = sum(closes) / n
        mv = sum(vols) / n
        cov = sum((closes[i] - mc) * (vols[i] - mv) for i in range(n)) / n
        sc = (sum((v - mc) ** 2 for v in closes) / n) ** 0.5
        sv = (sum((v - mv) ** 2 for v in vols) / n) ** 0.5
        return cov / (sc * sv) if sc > 0 and sv > 0 else None

    async def _factor_short(self, session, trade_date: str, dates: list, fw: dict) -> list:
        """短线选股池(持有20日/月度调仓)：反转F1 + 量价背离F2 + 成长F8，各15只。"""
        td42 = self._nth_date(dates, trade_date, 42)
        td20 = self._nth_date(dates, trade_date, 20)
        if not td42 or not td20:
            return []

        # 1. 候选：当日全市场非ST非688/920，JOIN 42日前close(反转) + 最新财务(F8成长) + 行业
        r = await session.execute(text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   (d.close - d42.close) / NULLIF(d42.close, 0) AS rev42,
                   fi.dt_netprofit_yoy, s.industry
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            JOIN (SELECT ts_code, close FROM stock_daily WHERE trade_date = :td42) d42 ON d42.ts_code = d.ts_code
            LEFT JOIN fina_indicator fi ON fi.ts_code = d.ts_code
                AND fi.end_date = (SELECT MAX(end_date) FROM fina_indicator fi2 WHERE fi2.ts_code = d.ts_code)
            WHERE d.trade_date = :td
              AND d.close > 0 AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
        """), {"td": trade_date, "td42": td42})
        cand = r.fetchall()
        if not cand:
            return []

        # 2. 拉近20日 close/volume，算价量相关(F2)
        r2 = await session.execute(text("""
            SELECT ts_code, close, volume FROM stock_daily
            WHERE trade_date > :td20 AND trade_date <= :td AND volume > 0
            ORDER BY ts_code, trade_date
        """), {"td20": td20, "td": trade_date})
        seq: dict = {}
        for ts_code, close, vol in r2.fetchall():
            seq.setdefault(ts_code, []).append((float(close), float(vol)))

        # 3. 构造 rows：(code, name, close, pct_chg, vol, rev42, corr, growth)，行业单独映射
        rows = []
        industry_map = {}
        for code, name, close, pct_chg, vol, rev42, growth, industry in cand:
            corr = self._corr_price_vol(seq.get(code, []))
            rows.append((code, name, close, pct_chg, vol, rev42, corr, growth))
            industry_map[code] = industry or ""

        items = score_and_rank(rows, fw["factor_short"], "短线选股(反转+量价背离+成长)", limit=60)
        for it in items:
            it["industry"] = industry_map.get(it.get("stock_code"), "")
        items = self._apply_industry_cap(items, fw["factor_short"].get("max_per_industry", 3))
        return await self._attach_risks(items)

    async def _factor_long(self, session, trade_date: str, dates: list, fw: dict) -> list:
        """长线选股池(持有60日/季度调仓)：量价背离F2 + 成长F8 + 现金流F7，各15只。"""
        td20 = self._nth_date(dates, trade_date, 20)
        if not td20:
            return []

        # 1. 候选：当日全市场非ST非688/920，JOIN 最新财务(F8净利增速/F7现金流) + 行业
        r = await session.execute(text("""
            SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume,
                   fi.dt_netprofit_yoy, fi.cfps_yoy, s.industry
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            LEFT JOIN fina_indicator fi ON fi.ts_code = d.ts_code
                AND fi.end_date = (SELECT MAX(end_date) FROM fina_indicator fi2 WHERE fi2.ts_code = d.ts_code)
            WHERE d.trade_date = :td
              AND d.close > 0 AND d.volume > 0
              AND d.ts_code NOT LIKE '%ST%' AND s.name NOT LIKE '%ST%'
              AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
        """), {"td": trade_date})
        cand = r.fetchall()
        if not cand:
            return []

        # 2. 拉近20日 close/volume，算价量相关(F2)
        r2 = await session.execute(text("""
            SELECT ts_code, close, volume FROM stock_daily
            WHERE trade_date > :td20 AND trade_date <= :td AND volume > 0
            ORDER BY ts_code, trade_date
        """), {"td20": td20, "td": trade_date})
        seq: dict = {}
        for ts_code, close, vol in r2.fetchall():
            seq.setdefault(ts_code, []).append((float(close), float(vol)))

        # 3. 构造 rows：(code, name, close, pct_chg, vol, corr, growth, cfps)，行业单独映射
        rows = []
        industry_map = {}
        for code, name, close, pct_chg, vol, growth, cfps, industry in cand:
            corr = self._corr_price_vol(seq.get(code, []))
            rows.append((code, name, close, pct_chg, vol, corr, growth, cfps))
            industry_map[code] = industry or ""

        items = score_and_rank(rows, fw["factor_long"], "长线选股(量价背离+成长+现金流)", limit=60)
        for it in items:
            it["industry"] = industry_map.get(it.get("stock_code"), "")
        items = self._apply_industry_cap(items, fw["factor_long"].get("max_per_industry", 3))
        return await self._attach_risks(items)

    @staticmethod
    def _apply_industry_cap(items: list, max_per_industry: int = 3, limit: int = 15) -> list:
        """行业分散：每个行业最多 max_per_industry 只，按得分顺延补录，凑满 limit 只。

        输入按综合分降序排列的候选(items 已排序)，输出行业分散后的前 limit 只。
        """
        result: list = []
        count: dict = {}
        for it in items:
            ind = it.get("industry") or "未分类"
            if count.get(ind, 0) >= max_per_industry:
                continue
            count[ind] = count.get(ind, 0) + 1
            result.append(it)
            if len(result) >= limit:
                break
        return result

    async def _attach_risks(self, items: list) -> list:
        """为选出的股票叠加风险信号标注(R1放量见顶/R2龙虎榜)，不改排序只加标注。

        风险信号是单股择时/风险预警(见 data/risk_signals.json)，独立于选股因子，
        不参与综合得分。触发则标注 risks 字段供前端 ⚠️ 提示。
        """
        if not items:
            return items
        from app.services.factor_engine import scan_risks
        for item in items:
            code = item.get("stock_code")
            if not code:
                continue
            try:
                risks = await scan_risks(code)
            except Exception:
                risks = []
            if risks:
                item["risks"] = [r["code"] for r in risks]
                item["risk_names"] = [r["name"] for r in risks]
        return items

    # ── 持久化 ──

    async def _persist_pool(self, pool_type: str, items: list, calc_date: str):
        async with async_session() as session:
            # 先删该日期该池旧结果，避免重跑时重复（INSERT OR REPLACE 依赖自增id主键，不会真正replace）
            await session.execute(text(
                "DELETE FROM stock_pool_results WHERE calc_date = :cd AND pool_type = :pt"
            ), {"cd": calc_date, "pt": pool_type})
            for i, item in enumerate(items):
                try:
                    await session.execute(text("""
                        INSERT INTO stock_pool_results
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
                            "risks": item.get("risks", []),
                            "risk_names": item.get("risk_names", []),
                        }),
                        "ir": item.get("inclusion_reason", ""),
                    })
                except Exception:
                    continue
            await session.commit()
