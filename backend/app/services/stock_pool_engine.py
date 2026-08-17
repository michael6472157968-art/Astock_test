"""选股池计算引擎——因子选股池(短线20日 + 长线60日)，基于SQLite真实数据。

收盘后运行一次，结果写入内存缓存。每池15只，前4行业均衡(4+4+4+3)。
统一使用 factor_weights.json 配置驱动的 score_and_rank()。
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


class StockPoolEngine:

    async def compute_all(self, trade_date: str = "") -> dict:
        # 加载因子权重配置
        with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            fw = json.load(f)

        if not trade_date:
            async with async_session() as sess:
                # 用「完整交易日」(>=50只) 而非裸 MAX：盘中同步进少量数据会让 MAX 跳到不完整的新交易日
                r = await sess.execute(text(
                    "SELECT trade_date FROM stock_daily GROUP BY trade_date "
                    "HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
                ))
                trade_date = r.scalar() or (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        # 找出最近60个完整交易日（给因子池算42/20日窗口用）
        async with async_session() as sess:
            r = await sess.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60"
            ), {"td": trade_date})
            all_dates = [row[0] for row in r.fetchall()]

        async with async_session() as session:
            factor_short = await self._factor_short(session, trade_date, all_dates, fw)
            factor_long = await self._factor_long(session, trade_date, all_dates, fw)

        pools = {
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

        items = score_and_rank(rows, fw["factor_short"], "短线选股(反转+量价背离+成长)", limit=5000)
        for it in items:
            it["industry"] = industry_map.get(it.get("stock_code"), "")
        items = self._apply_top_industries(items, fw["factor_short"].get("top_industries", 4))
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

        items = score_and_rank(rows, fw["factor_long"], "长线选股(量价背离+成长+现金流)", limit=5000)
        for it in items:
            it["industry"] = industry_map.get(it.get("stock_code"), "")
        items = self._apply_top_industries(items, fw["factor_long"].get("top_industries", 4))
        return await self._attach_risks(items)

    @staticmethod
    def _apply_top_industries(items: list, top_n: int = 4, limit: int = 15) -> list:
        """选综合分最高的前 top_n 个行业(按行业最高分排序)，每行业均衡约 limit/top_n 只。

        先确定前 N 行业，再从这些行业按综合分各取约 ceil(limit/N) 只，凑满 limit 只，
        避免高分行业一家独大(如化工原料占一半)。
        """
        if not items:
            return items
        best: dict = {}
        for it in items:
            ind = it.get("industry") or "未分类"
            s = it.get("score") or 0
            if s > best.get(ind, -1e9):
                best[ind] = s
        top_inds = {ind for ind, _ in sorted(best.items(), key=lambda x: -x[1])[:top_n]}
        per = -(-limit // top_n)  # 每行业上限 = ceil(limit / top_n)，15/4 → 4
        result: list = []
        count: dict = {}
        for it in items:
            ind = it.get("industry") or "未分类"
            if ind not in top_inds:
                continue
            if count.get(ind, 0) >= per:
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
