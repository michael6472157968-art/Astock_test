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
from app.services.factor_lib import clip, minmax_norm, rank_pct, winsorize_mad, zscore

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
            r = await session.execute(text(
                "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
                "GROUP BY trade_date HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 60"
            ), {"td": trade_date})
            all_dates = [row[0] for row in r.fetchall()]
            if not all_dates:
                return {}

            # ── 去重优先级链：T+3追涨 > T+3低吸 > T+7追涨 > T+7低吸 ──
            # 超卖例外：dist_from_low<=10 或 drawdown>=20 可跨池保留

            t3m = await self._t3_momentum(session, trade_date, all_dates)
            excluded = {item["stock_code"] for item in t3m}

            # T+3低吸：排除已在T+3追涨中的股票（超卖除外）
            t3d_candidates = await self._t3_dip_raw(session, trade_date, all_dates)
            t3d = self._dedup_pool(t3d_candidates, excluded, "dist_from_low", 10)
            excluded = excluded | {item["stock_code"] for item in t3d}

            # T+7追涨：排除已在T+3两池中的股票（无超卖豁免，momentum风格无超卖字段）
            t7m_candidates = await self._t7_momentum_raw(session, trade_date, all_dates)
            t7m = self._dedup_pool(t7m_candidates, excluded, None, None)
            excluded = excluded | {item["stock_code"] for item in t7m}

            # T+7低吸：排除所有高优先级池中的股票（超卖除外）
            t7d_candidates = await self._t7_dip_raw(session, trade_date, all_dates)
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
    async def _t3_momentum(self, session, trade_date: str, dates: list) -> list:
        """强势股短期惯性上冲。近3日涨幅>3%，今日>1%，站上MA5，量比>1.2。"""
        items = await self._t3_momentum_raw(session, trade_date, dates)
        return items[:POOL_SIZE]

    async def _t3_momentum_raw(self, session, trade_date: str, dates: list):
        """返回 T+3追涨 全部候选（最多30条），供去重使用"""
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
        return self._score_rows(r, "T+3追涨", ["chg3", "vol_ratio", "pct_chg", "turnover", "pe", "pb", "total_mv"],
                                [0.25, 0.15, 0.15, 0.10, 0.15, 0.10, 0.10], limit=POOL_SIZE * 3)

    # ── T+3 低吸 ──
    async def _t3_dip(self, session, trade_date: str, dates: list) -> list:
        """回调企稳后反弹。"""
        items = await self._t3_dip_raw(session, trade_date, dates)
        return items[:POOL_SIZE]

    async def _t3_dip_raw(self, session, trade_date: str, dates: list):
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
        return self._score_rows(r, "T+3低吸", ["chg5", "vol_ratio", "dist_from_low", "pct_chg", "pe", "pb", "total_mv"],
                                [0.20, 0.15, 0.20, 0.15, 0.12, 0.08, 0.10], oversold_field="dist_from_low",
                                limit=POOL_SIZE * 3)

    # ── T+7 追涨 ──
    async def _t7_momentum(self, session, trade_date: str, dates: list) -> list:
        """趋势已确立顺势持股。"""
        items = await self._t7_momentum_raw(session, trade_date, dates)
        return items[:POOL_SIZE]

    async def _t7_momentum_raw(self, session, trade_date: str, dates: list):
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
        return self._score_rows(r, "T+7追涨", ["chg10", "ma_slope", "pct_chg", "turnover", "pe", "pb", "total_mv"],
                                [0.25, 0.15, 0.15, 0.10, 0.15, 0.10, 0.10], limit=POOL_SIZE * 3)

    # ── T+7 低吸 ──
    async def _t7_dip(self, session, trade_date: str, dates: list) -> list:
        """中期回调修复机会。"""
        items = await self._t7_dip_raw(session, trade_date, dates)
        return items[:POOL_SIZE]

    async def _t7_dip_raw(self, session, trade_date: str, dates: list):
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
        return self._score_rows(r, "T+7低吸", ["drawdown", "chg3_recent", "chg10", "pct_chg", "pe", "pb", "total_mv"],
                                [0.25, 0.15, 0.15, 0.10, 0.15, 0.10, 0.10], oversold_field="drawdown",
                                limit=POOL_SIZE * 3)

    # ── 评分排序 ──

    # 基础因子列索引（SQL SELECT 固定位置: col0=ts_code, col1=name, col2=close,
    # col3=pct_chg, col4=volume, col5+=derived fields + daily_basic fields）
    _BASE_COLS = 5

    # 需反向排名的字段（原始值越小得分越高）
    _INVERT_FIELDS: set[str] = {"dist_from_low"}

    # zscore 归一化因子：基本面因子用 winsorize_mad → zscore → clip → minmax
    # 比 rank_pct 更能反映真实分位数差异（PE从15到30的gap比第80到第90百分位更有意义）
    _ZSCORE_FIELDS: set[str] = {"pe", "pb", "total_mv"}

    def _score_rows(self, rows, reason_prefix: str, fields: list[str],
                    weights: list[float], oversold_field: str | None = None,
                    limit: int = POOL_SIZE) -> list:
        """横截面评分：基本面因子(zscore) + 技术因子(rank_pct) → 加权排序。

        归一化策略：
        - 基本面因子（PE/PB/市值）：winsorize_mad(5σ) → zscore → clip[-3,3] → minmax[0,1]
          对于反向因子（PE/PB越低越好），用 (1 - score) 翻转
        - 技术因子（涨跌幅/量比/换手/均线斜率）：rank_pct 横截面排名
        - vol_ratio 特殊处理：按 |val - 1.0| 排名（越接近1越高分）
        """
        if not rows:
            return []

        # 提取每个因子值，按字段名而非列索引对齐
        field_vals_all: dict[str, list[float]] = {f: [] for f in fields}
        raw_items: list[dict] = []
        for row in rows:
            fv = {}
            for i, f in enumerate(fields):
                idx = self._BASE_COLS + i
                val = float(row[idx]) if idx < len(row) and row[idx] is not None else 0.0
                fv[f] = val
                field_vals_all[f].append(val)
            raw_items.append({
                "code": row[0], "name": row[1],
                "close": float(row[2]) if row[2] is not None else None,
                "pct_chg": float(row[3]) if row[3] is not None else 0,
                "vol": float(row[4]) if len(row) > 4 and row[4] is not None else 0,
                "fv": fv,
            })

        # 每个因子做归一化
        norm_cache: dict[str, list[float]] = {}
        for f in fields:
            vals = field_vals_all[f]
            if f == "vol_ratio":
                # 按 |val - 1.0| 排名（越小越接近1.0），反向取分
                dist = [abs(v - 1.0) for v in vals]
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct(dist)]
            elif f in self._ZSCORE_FIELDS:
                # 基本面因子：winsorize → zscore → clip → minmax
                w = winsorize_mad(vals, 5.0)
                z = zscore(w)
                c = clip(z, -3.0, 3.0)
                normed = minmax_norm(c)
                # PE/PB/市值 越低越好 → 反向
                norm_cache[f] = [round(1.0 - v, 6) for v in normed]
            elif f in self._INVERT_FIELDS:
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct(vals)]
            else:
                norm_cache[f] = rank_pct(vals)

        # 加权评分
        scored = []
        for idx, item in enumerate(raw_items):
            score = sum(norm_cache[f][idx] * w for f, w in zip(fields, weights))
            entry = {
                "stock_code": item["code"],
                "stock_name": item["name"],
                "close": item["close"],
                "change_pct": round(item["pct_chg"], 2),
                "score": round(score * 100, 1),
                "volume": item["vol"],
                "inclusion_reason": f"{reason_prefix} 评分{round(score*100,1)}",
                "mode": reason_prefix,
            }
            if oversold_field and oversold_field in item["fv"]:
                entry["_raw_oversold_val"] = round(item["fv"][oversold_field], 2)
            scored.append(entry)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

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
