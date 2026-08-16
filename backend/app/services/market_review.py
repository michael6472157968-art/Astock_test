"""每日复盘简报引擎——8维度并行计算，零新API依赖。

Phase 1: 基于现有8张DB表 + DeepSeek AI总结。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

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

        results = await asyncio.gather(
            self._dim_market_temperature(trade_date),
            self._dim_smart_money(trade_date),
            self._dim_institutional_flow(trade_date),
            self._dim_anomaly_signals(trade_date),
            self._dim_strategy_pools(trade_date),
            return_exceptions=True,
        )

        dim_keys = [
            "temperature", "smart_money",
            "institutional", "anomaly", "strategy_pools",
        ]
        content = {}
        for k, r in zip(dim_keys, results):
            if isinstance(r, Exception):
                logger.warning(f"Dimension '{k}' failed: {r}")
                content[k] = {"error": str(r)}
            else:
                content[k] = r or {}

        content["ai_summary"] = await self._dim_ai_summary(trade_date, content)

        review_data = {"date": trade_date, "content": content}
        await cache_set(f"review:{trade_date}", review_data, ttl=_settings.cache_offline_ttl)
        logger.info(f"Market review (8-dims) computed for {trade_date}")
        return review_data

    # ═══════════════════════════════════════════════════════════════
    # Dimension 1: 大盘温度
    # ═══════════════════════════════════════════════════════════════

    async def _dim_market_temperature(self, td: str) -> dict:
        async with async_session() as sess:
            r = await sess.execute(text("""
                SELECT COUNT(*),
                       SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END),
                       ROUND(AVG(pct_chg), 2),
                       ROUND(MAX(pct_chg), 2),
                       ROUND(MIN(pct_chg), 2),
                       ROUND(SUM(amount), 2)
                FROM stock_daily WHERE trade_date = :td
            """), {"td": td})
            row = r.fetchone()

            total = int(row[0] or 0)
            if not total:
                return {"total": 0, "summary": "无数据"}

            up_count = int(row[1] or 0)
            flat_count = int(row[2] or 0)
            down_count = int(row[3] or 0)
            avg_pct = float(row[4] or 0)
            max_pct = float(row[5] or 0)
            min_pct = float(row[6] or 0)
            total_amount = float(row[7] or 0)
            up_ratio = round(up_count / total * 100, 1) if total else 0

            # 5日均量对比
            r_prev = await sess.execute(text("""
                SELECT ROUND(SUM(amount), 2) FROM stock_daily
                WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < :td)
            """), {"td": td})
            prev_amount = float((r_prev.first() or [0])[0] or 0)
            amount_change = round((total_amount - prev_amount) / prev_amount * 100, 1) if prev_amount else 0

            # 换手率
            r_db = await sess.execute(text("""
                SELECT ROUND(AVG(turnover_rate), 2)
                FROM daily_basic WHERE trade_date = :td
            """), {"td": td})
            db_row = r_db.first()
            avg_turnover = float(db_row[0] or 0) if db_row else 0

            # 涨跌停
            limit_up, limit_down = await _fetch_limit_up_down(sess, td)

            # 宽度标签
            if up_ratio >= 80:
                width_label = "强势普涨"
            elif up_ratio >= 60:
                width_label = "偏多震荡"
            elif up_ratio >= 40:
                width_label = "多空均衡"
            elif up_ratio >= 20:
                width_label = "偏空震荡"
            else:
                width_label = "恐慌普跌"

            sentiment = min(100, max(0, int(up_ratio + limit_up * 0.5 - limit_down * 1.5)))

            # TOP5 涨跌 + 行业
            top_gainers = await _query_top_movers(sess, td, "DESC", 5)
            top_losers = await _query_top_movers(sess, td, "ASC", 5)
            top_sectors = await _query_top_sectors(sess, td, 5)

        return {
            "total": total,
            "up_count": up_count, "down_count": down_count, "flat_count": flat_count,
            "up_ratio": up_ratio,
            "limit_up": limit_up, "limit_down": limit_down,
            "avg_pct": avg_pct, "max_pct": max_pct, "min_pct": min_pct,
            "total_amount_yi": round(total_amount / 1e8, 0),
            "amount_change_pct": amount_change,
            "avg_turnover": avg_turnover,
            "width_label": width_label,
            "sentiment_score": sentiment,
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "top_sectors": top_sectors,
            "summary": f"全市场{total}只，涨{up_count}平{flat_count}跌{down_count}({up_ratio}%↑)，涨停{limit_up}只跌停{limit_down}只，均涨{avg_pct:+.2f}%，成交{round(total_amount/1e8,0):.0f}亿"
        }

    # ═══════════════════════════════════════════════════════════════
    # Dimension 2: 聪明钱共识 + 北向活跃板块
    # ═══════════════════════════════════════════════════════════════

    async def _dim_smart_money(self, td: str) -> dict:
        result: dict = {"northbound_net_yi": 0, "northbound_5d_trend": [],
                         "smart_money_label": "数据不足", "northbound": None,
                         "northbound_sectors": []}

        async with async_session() as sess:
            try:
                r = await sess.execute(text(
                    "SELECT trade_date, north_money FROM moneyflow_hsgt "
                    "WHERE trade_date <= :td ORDER BY trade_date DESC LIMIT 5"
                ), {"td": td})
                rows = list(r)
                if rows:
                    latest = rows[0]
                    net = round(float(latest[1] or 0) * 1e4, 2)
                    result["northbound_net_yi"] = round(net / 1e8, 2)
                    result["northbound_5d_trend"] = [
                        {"date": str(r[0]), "value": round(float(r[1] or 0) * 1e4 / 1e8, 2)}
                        for r in rows
                    ][::-1]
                    trend_values = [float(r[1] or 0) for r in rows]
                    pos_days = sum(1 for v in trend_values if v > 0)
                    if pos_days >= 4:
                        result["smart_money_label"] = "北向连续大幅流入"
                    elif pos_days >= 2:
                        result["smart_money_label"] = "北向持续流入"
                    elif net > 0:
                        result["smart_money_label"] = "北向净流入"
                    elif net < 0:
                        result["smart_money_label"] = "北向净流出"
                    else:
                        result["smart_money_label"] = "北向持平"
                    result["northbound"] = {
                        "date": latest[0], "net_in": round(net, 2),
                        "recent": [{"date": r[0], "net_in": round(float(r[1] or 0) * 1e4, 2)}
                                   for r in rows][::-1]
                    }
            except Exception as e:
                logger.warning(f"smart_money northbound: {e}")

            # 北向活跃板块占比 (hsgt_top10 amount 按行业聚合)
            try:
                r = await sess.execute(text(
                    "SELECT ts_code, amount FROM hsgt_top10 WHERE trade_date = :td"
                ), {"td": td})
                top10_rows = list(r)
                if top10_rows:
                    sector_amounts: dict[str, float] = {}
                    total_amt = 0.0
                    for ts_code, amount in top10_rows:
                        sr = await sess.execute(
                            text("SELECT industry FROM stocks WHERE ts_code = :c"),
                            {"c": ts_code})
                        ind_row = sr.first()
                        industry = (ind_row[0] or "其他") if ind_row else "其他"
                        amt_val = float(amount or 0) / 1e8
                        sector_amounts[industry] = sector_amounts.get(industry, 0) + amt_val
                        total_amt += amt_val
                    result["northbound_sectors"] = [
                        {"name": k, "value": round(v, 1),
                         "pct": round(v / total_amt * 100, 1) if total_amt else 0}
                        for k, v in sorted(sector_amounts.items(), key=lambda x: -x[1])[:8]
                    ]
            except Exception as e:
                logger.warning(f"smart_money hsgt_top10 sectors: {e}")

        return result

    # ═══════════════════════════════════════════════════════════════
    # Dimension 3: 机构持仓变动 (top10_floatholders + stk_holdernumber)
    # ═══════════════════════════════════════════════════════════════

    async def _dim_institutional_flow(self, td: str) -> dict:
        """基于 top10_floatholders + stk_holdernumber 真实数据。"""
        async with async_session() as sess:
            # 机构增持/新进——从最新季报十大流通股东中筛选
            r = await sess.execute(text("""
                SELECT h.ts_code, s.name, h.holder_name, h.hold_ratio,
                       h.hold_float_ratio, h.hold_change
                FROM top10_floatholders h
                JOIN stocks s ON s.ts_code = h.ts_code
                WHERE h.end_date = (SELECT MAX(end_date) FROM top10_floatholders)
                  AND h.holder_type NOT LIKE '%自然人%'
                  AND h.hold_ratio > 1
                ORDER BY h.hold_ratio DESC LIMIT 10
            """))
            top_holders = [
                {"ts_code": rh[0], "name": rh[1], "holder": rh[2],
                 "hold_ratio": round(float(rh[3] or 0), 2),
                 "float_ratio": round(float(rh[4] or 0), 2),
                 "change": float(rh[5] or 0)}
                for rh in r
            ]

            # 股东户数骤降(筹码集中)——取最近2个有大量数据的日期对比
            r2 = await sess.execute(text("""
                WITH dates AS (
                    SELECT end_date, COUNT(*) as n FROM stk_holdernumber
                    GROUP BY end_date HAVING n >= 500 ORDER BY end_date DESC LIMIT 2
                ),
                d_vals AS (SELECT end_date, ROW_NUMBER() OVER (ORDER BY end_date DESC) as rn FROM dates),
                d1 AS (SELECT end_date FROM d_vals WHERE rn = 1),
                d2 AS (SELECT end_date FROM d_vals WHERE rn = 2),
                latest AS (
                    SELECT ts_code, holder_num FROM stk_holdernumber WHERE end_date = (SELECT end_date FROM d1)
                ),
                prev AS (
                    SELECT ts_code, holder_num FROM stk_holdernumber WHERE end_date = (SELECT end_date FROM d2)
                )
                SELECT DISTINCT l.ts_code, s.name, l.holder_num, p.holder_num,
                       ROUND((l.holder_num - p.holder_num) * 100.0 / NULLIF(p.holder_num, 0), 1)
                FROM latest l
                JOIN prev p ON p.ts_code = l.ts_code
                JOIN stocks s ON s.ts_code = l.ts_code
                WHERE p.holder_num > 0 AND l.holder_num < p.holder_num * 0.85
                ORDER BY (l.holder_num - p.holder_num) * 100.0 / p.holder_num ASC
                LIMIT 10
            """))
            concentration = [
                {"ts_code": rc[0], "name": rc[1],
                 "holders_now": int(rc[2] or 0), "holders_prev": int(rc[3] or 0),
                 "chg_pct": round(float(rc[4] or 0), 1)}
                for rc in r2
            ]

        return {
            "top_holders": top_holders,
            "concentration": concentration,
            "note": "基于Tushare top10_floatholders + stk_holdernumber 真实数据",
        }

    # ═══════════════════════════════════════════════════════════════
    # Dimension 6: 异常信号 (纯本地计算，零API)
    # ═══════════════════════════════════════════════════════════════

    async def _dim_anomaly_signals(self, td: str) -> dict:
        async with async_session() as sess:
            # 高换手超10%且非涨停
            r = await sess.execute(text(
                "SELECT d.ts_code, s.name, db.turnover_rate, d.pct_chg "
                "FROM daily_basic db "
                "JOIN stock_daily d ON d.ts_code = db.ts_code AND d.trade_date = db.trade_date "
                "JOIN stocks s ON s.ts_code = db.ts_code "
                "WHERE db.trade_date = :td AND db.turnover_rate > 10 AND d.pct_chg < 9 "
                "ORDER BY db.turnover_rate DESC LIMIT 10"
            ), {"td": td})
            high_turnover = [
                {"ts_code": rh[0], "name": rh[1], "turnover": round(float(rh[2] or 0), 2),
                 "pct_chg": round(float(rh[3] or 0), 2)}
                for rh in r
            ]

            # 量比>3 (当日量/5日均量)
            r2 = await sess.execute(text("""
                SELECT d.ts_code, s.name, d.volume, d.pct_chg,
                       ROUND(d.volume / NULLIF(avg5.avg_vol, 0), 1) as vol_ratio
                FROM stock_daily d
                JOIN stocks s ON s.ts_code = d.ts_code
                LEFT JOIN (
                    SELECT ts_code, AVG(volume) as avg_vol
                    FROM stock_daily
                    WHERE trade_date < :td AND trade_date >= (
                        SELECT MAX(trade_date) FROM (
                            SELECT trade_date FROM stock_daily
                            WHERE trade_date < :td ORDER BY trade_date DESC LIMIT 1 OFFSET 4
                        )
                    )
                    GROUP BY ts_code
                ) avg5 ON avg5.ts_code = d.ts_code
                WHERE d.trade_date = :td
                 AND ROUND(d.volume / NULLIF(avg5.avg_vol, 0), 1) > 3
                ORDER BY vol_ratio DESC LIMIT 10
            """), {"td": td})
            volume_surge = [
                {"ts_code": rv[0], "name": rv[1], "volume": int(rv[2] or 0),
                 "pct_chg": round(float(rv[3] or 0), 2), "vol_ratio": float(rv[4] or 0)}
                for rv in r2
            ]

            # 5日急涨>20%
            r3 = await sess.execute(text("""
                WITH latest AS (
                    SELECT ts_code, close, pct_chg FROM stock_daily WHERE trade_date = :td
                ),
                prev5 AS (
                    SELECT ts_code, close FROM stock_daily WHERE trade_date = (
                        SELECT MAX(trade_date) FROM (
                            SELECT trade_date FROM stock_daily
                            WHERE trade_date < :td ORDER BY trade_date DESC LIMIT 1 OFFSET 4
                        )
                    )
                )
                SELECT l.ts_code, s.name, l.pct_chg,
                       ROUND((l.close - p.close) / NULLIF(p.close, 0) * 100, 1) as chg5
                FROM latest l
                JOIN prev5 p ON p.ts_code = l.ts_code
                JOIN stocks s ON s.ts_code = l.ts_code
                WHERE ROUND((l.close - p.close) / NULLIF(p.close, 0) * 100, 1) > 20
                ORDER BY chg5 DESC LIMIT 10
            """), {"td": td})
            rapid_rise = [
                {"ts_code": rr[0], "name": rr[1], "pct_chg_today": round(float(rr[2] or 0), 2),
                 "chg_5d": round(float(rr[3] or 0), 2)}
                for rr in r3
            ]

            # 5日急跌<-20%
            r4 = await sess.execute(text("""
                WITH latest AS (
                    SELECT ts_code, close, pct_chg FROM stock_daily WHERE trade_date = :td
                ),
                prev5 AS (
                    SELECT ts_code, close FROM stock_daily WHERE trade_date = (
                        SELECT MAX(trade_date) FROM (
                            SELECT trade_date FROM stock_daily
                            WHERE trade_date < :td ORDER BY trade_date DESC LIMIT 1 OFFSET 4
                        )
                    )
                )
                SELECT l.ts_code, s.name, l.pct_chg,
                       ROUND((l.close - p.close) / NULLIF(p.close, 0) * 100, 1) as chg5
                FROM latest l
                JOIN prev5 p ON p.ts_code = l.ts_code
                JOIN stocks s ON s.ts_code = l.ts_code
                WHERE ROUND((l.close - p.close) / NULLIF(p.close, 0) * 100, 1) < -20
                ORDER BY chg5 ASC LIMIT 10
            """), {"td": td})
            rapid_drop = [
                {"ts_code": rd[0], "name": rd[1], "pct_chg_today": round(float(rd[2] or 0), 2),
                 "chg_5d": round(float(rd[3] or 0), 2)}
                for rd in r4
            ]

            # 极端缩量 (量比<0.3)
            r5 = await sess.execute(text("""
                SELECT d.ts_code, s.name, d.volume, d.pct_chg,
                       ROUND(d.volume / NULLIF(avg5.avg_vol, 0), 1) as vol_ratio
                FROM stock_daily d
                JOIN stocks s ON s.ts_code = d.ts_code
                LEFT JOIN (
                    SELECT ts_code, AVG(volume) as avg_vol
                    FROM stock_daily
                    WHERE trade_date < :td AND trade_date >= (
                        SELECT MAX(trade_date) FROM (
                            SELECT trade_date FROM stock_daily
                            WHERE trade_date < :td ORDER BY trade_date DESC LIMIT 1 OFFSET 4
                        )
                    )
                    GROUP BY ts_code
                ) avg5 ON avg5.ts_code = d.ts_code
                WHERE d.trade_date = :td
                 AND ROUND(d.volume / NULLIF(avg5.avg_vol, 0), 1) < 0.3
                   AND ROUND(d.volume / NULLIF(avg5.avg_vol, 0), 1) > 0
                ORDER BY vol_ratio ASC LIMIT 10
            """), {"td": td})
            volume_shrink = [
                {"ts_code": rv[0], "name": rv[1], "volume": int(rv[2] or 0),
                 "pct_chg": round(float(rv[3] or 0), 2), "vol_ratio": float(rv[4] or 0)}
                for rv in r5
            ]

        return {
            "high_turnover": high_turnover,
            "volume_surge": volume_surge,
            "volume_shrink": volume_shrink,
            "rapid_rise_5d": rapid_rise,
            "rapid_drop_5d": rapid_drop,
        }

    # ═══════════════════════════════════════════════════════════════
    # Dimension 7: 策略池表现
    # ═══════════════════════════════════════════════════════════════

    async def _dim_strategy_pools(self, td: str) -> dict:
        import json

        async with async_session() as sess:
            r = await sess.execute(text(
                "SELECT pool_type, ts_code, stock_name, market_data_json, rank_in_pool "
                "FROM stock_pool_results WHERE calc_date = :td "
                "ORDER BY pool_type, rank_in_pool"
            ), {"td": td})
            rows = list(r)

        if not rows:
            prev_td = await self._prev_trade_date(td)
            if prev_td:
                async with async_session() as sess:
                    r = await sess.execute(text(
                        "SELECT pool_type, ts_code, stock_name, market_data_json, rank_in_pool "
                        "FROM stock_pool_results WHERE calc_date = :td "
                        "ORDER BY pool_type, rank_in_pool"
                    ), {"td": prev_td})
                    rows = list(r)

        # 按 pool_type 分组，去重（同ts_code取最低rank）
        pool_stocks: dict[str, dict[str, dict]] = {}
        for pool_type, ts_code, stock_name, data_json, rank in rows:
            try:
                data = json.loads(data_json) if isinstance(data_json, str) else (data_json or {})
                pct = data.get("change_pct", data.get("pct_chg", data.get("score", 0)))
                pct_val = float(pct) if isinstance(pct, (int, float)) else 0

                if pool_type not in pool_stocks:
                    pool_stocks[pool_type] = {}
                if ts_code not in pool_stocks[pool_type] or rank < pool_stocks[pool_type][ts_code].get("_rank", 999):
                    pool_stocks[pool_type][ts_code] = {
                        "name": stock_name or ts_code, "code": ts_code,
                        "pct_chg": pct_val, "_rank": rank,
                    }
            except Exception as e:
                logger.warning(f"Strategy pool row parse error: {e}")

        result = {}
        for k, stocks in pool_stocks.items():
            sorted_stocks = sorted(stocks.values(), key=lambda x: x["_rank"])
            pcts = [s["pct_chg"] for s in sorted_stocks]
            top3 = sorted_stocks[:3]
            for s in top3:
                del s["_rank"]
            result[k] = {
                "top3": top3,
                "avg_pct": round(sum(pcts) / len(pcts), 2) if pcts else 0,
            }

        return {
            "pools": result,
            "calc_date": td,
            "note": "策略池当日选股表现" if rows else f"{td} 无选股数据",
        }

    # ═══════════════════════════════════════════════════════════════
    # Dimension 8: AI一句话总结
    # ═══════════════════════════════════════════════════════════════

    async def _dim_ai_summary(self, td: str, dims: dict) -> dict:
        """调用 DeepSeek 整合7维度数据生成100-150字摘要。"""
        try:
            from app.services.ai_analysis import _get_client

            summary_input = self._build_ai_summary_input(td, dims)

            system_prompt = (
                "你是一个A股市场复盘助手。你的职责是将提供的市场数据整合为一段100-150字的客观摘要。\n"
                "严格约束：\n"
                "- 只陈述数据反映的客观事实，不预测未来走势\n"
                "- 不使用\"建议买入\"\"建议卖出\"等投资决策语言\n"
                "- 突出最显著的3-5个信号，不重要信息一笔带过\n"
                "- 风格: 专业、简洁、数据驱动"
            )

            user_prompt = f"请根据以下{td}市场数据，生成一段100-150字的市场复盘总结：\n\n{summary_input}"

            client = _get_client()
            response = await client.chat.completions.create(
                model=_settings.deepseek_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=300,
            )
            text = response.choices[0].message.content or ""
            return {
                "text": text.strip(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": _settings.deepseek_model,
            }
        except Exception as e:
            logger.warning(f"AI summary failed: {e}")
            return {
                "text": self._build_fallback_summary(dims),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": "fallback",
                "note": f"AI调用失败({e})，使用规则引擎生成摘要",
            }

    def _build_ai_summary_input(self, td: str, dims: dict) -> str:
        lines = []
        t = dims.get("temperature", {})
        if t and not t.get("error"):
            lines.append(
                f"大盘: {t.get('total', 0)}只, 涨跌比{t.get('up_ratio', 0)}%, "
                f"涨停{t.get('limit_up', 0)}跌停{t.get('limit_down', 0)}, "
                f"均涨{t.get('avg_pct', 0):+.2f}%, 成交{t.get('total_amount_yi', 0):.0f}亿, "
                f"标签: {t.get('width_label', '')}"
            )

        sm = dims.get("smart_money", {})
        if sm and not sm.get("error"):
            lines.append(
                f"北向: {sm.get('northbound_net_yi', 0):+.1f}亿, "
                f"标签: {sm.get('smart_money_label', '')}"
            )

        inst = dims.get("institutional", {})
        if inst and not inst.get("error"):
            top = inst.get("top_holders", [])[:3]
            conc = inst.get("concentration", [])
            lines.append(
                f"机构: {len(top)}只重仓, "
                f"筹码集中{len(conc)}只"
            )

        an = dims.get("anomaly", {})
        if an and not an.get("error"):
            lines.append(
                f"异常: 高换手{len(an.get('high_turnover', []))}只, "
                f"急涨{len(an.get('rapid_rise_5d', []))}只, "
                f"急跌{len(an.get('rapid_drop_5d', []))}只"
            )

        sp = dims.get("strategy_pools", {})
        if sp and not sp.get("error"):
            pools = sp.get("pools", {})
            pool_lines = []
            for k, v in list(pools.items())[:4]:
                if v.get("avg_pct") is not None:
                    pool_lines.append(f"{k}:{v['avg_pct']:+.2f}%")
            if pool_lines:
                lines.append(f"策略池: {'; '.join(pool_lines)}")

        return "\n".join(lines)

    def _build_fallback_summary(self, dims: dict) -> str:
        """AI调用失败时的规则引擎摘要。"""
        parts = []
        t = dims.get("temperature", {})
        if t and not t.get("error") and t.get("total"):
            label = t.get("width_label", "")
            parts.append(f"今日市场{label}，全市场{t.get('total',0)}只股票中{t.get('up_ratio',0)}%上涨，"
                        f"涨停{t.get('limit_up',0)}只跌停{t.get('limit_down',0)}只，"
                        f"成交额{t.get('total_amount_yi',0):.0f}亿。")

        sm = dims.get("smart_money", {})
        if sm and not sm.get("error"):
            nb = sm.get("northbound_net_yi", 0)
            if nb != 0:
                direction = "流入" if nb > 0 else "流出"
                parts.append(f"北向资金{direction}{abs(nb):.1f}亿，{sm.get('smart_money_label','')}。")

        inst = dims.get("institutional", {})
        if inst and not inst.get("error"):
            conc = inst.get("concentration", [])
            if conc:
                parts.append(f"筹码集中股{len(conc)}只，股东户数大幅下降。")

        an = dims.get("anomaly", {})
        if an and not an.get("error"):
            surge = len(an.get("rapid_rise_5d", []))
            drop = len(an.get("rapid_drop_5d", []))
            if surge or drop:
                parts.append(f"异常信号：{surge}只急涨，{drop}只急跌。")

        sp = dims.get("strategy_pools", {})
        if sp and not sp.get("error"):
            pools = sp.get("pools", {})
            best = None
            best_pct = -999
            for k, v in pools.items():
                if v.get("avg_pct", -999) > best_pct:
                    best_pct = v.get("avg_pct", -999)
                    best = k
            if best:
                parts.append(f"策略池中{best}表现最佳，平均收益{best_pct:+.2f}%。")

        return " ".join(parts) if parts else "暂无足够数据生成摘要。"

    # ═══════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════

    async def _latest_date(self) -> str:
        async with async_session() as s:
            r = await s.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
            return r.scalar()

    async def _prev_trade_date(self, td: str) -> str | None:
        async with async_session() as s:
            r = await s.execute(text(
                "SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < :td"
            ), {"td": td})
            return r.scalar()


# ── 共享辅助函数 ──

async def _fetch_limit_up_down(sess, td: str) -> tuple[int, int]:
    """获取涨跌停数量，优先从 limit_list_records 读取。"""
    r = await sess.execute(text(
        "SELECT COUNT(*) FILTER (WHERE limit_type = 'U'), "
        "COUNT(*) FILTER (WHERE limit_type = 'D') "
        "FROM limit_list_records WHERE trade_date = :td"
    ), {"td": td})
    lr = r.first()
    if lr and (lr[0] or lr[1]):
        return int(lr[0] or 0), int(lr[1] or 0)
    # 降级
    r_fb = await sess.execute(text(
        "SELECT COUNT(*) FILTER (WHERE pct_chg >= 9.8) as up, "
        "COUNT(*) FILTER (WHERE pct_chg <= -9.8) as down "
        "FROM stock_daily WHERE trade_date = :td"
    ), {"td": td})
    fb = r_fb.first()
    return (int(fb[0] or 0), int(fb[1] or 0)) if fb else (0, 0)


async def _query_top_movers(sess, td: str, order: str, limit: int = 5) -> list[dict]:
    direction = ">" if order == "DESC" else "<"
    # 排除上市不足30天的新股（name like 'N%' or 'C%'）及当日涨跌幅>200的异常值（新股无涨跌停限制）
    r = await sess.execute(text(
        f"SELECT DISTINCT s.name, d.pct_chg, s.industry "
        f"FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code "
        f"WHERE d.trade_date = :td AND d.pct_chg {direction} 0 "
        f"AND s.name NOT LIKE 'N%' AND s.name NOT LIKE 'C%' "
        f"AND d.pct_chg BETWEEN -21 AND 21 "
        f"ORDER BY d.pct_chg {order} LIMIT :lim"
    ), {"td": td, "lim": limit})
    return [{"name": x[0], "pct": round(float(x[1]), 2), "industry": x[2]} for x in r]


async def _query_top_sectors(sess, td: str, limit: int = 5) -> list[dict]:
    r = await sess.execute(text(
        "SELECT s.industry, ROUND(AVG(d.pct_chg), 2) "
        "FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code "
        "WHERE d.trade_date = :td AND s.industry != '' "
        "GROUP BY s.industry HAVING COUNT(*) >= 5 "
        "ORDER BY AVG(d.pct_chg) DESC LIMIT :lim"
    ), {"td": td, "lim": limit})
    return [{"name": x[0], "avg_pct": round(float(x[1]), 2)} for x in r]


async def _count_board_type(sess, td: str, board_type: str) -> int:
    """统计涨停板类型: first(首板) / lianban(连板) / zhaban(炸板)。"""
    if board_type == "first":
        r = await sess.execute(text(
            "SELECT COUNT(*) FROM limit_list_records "
            "WHERE trade_date = :td AND limit_type = 'U' "
            "AND (lu_desc IS NULL OR lu_desc = '' OR lu_desc = '首板')"
        ), {"td": td})
    else:
        r = await sess.execute(text(
            "SELECT COUNT(*) FROM limit_list_records "
            "WHERE trade_date = :td AND limit_type = 'U' "
            "AND lu_desc IS NOT NULL AND lu_desc != '' AND lu_desc != '首板'"
        ), {"td": td})
    return (r.first() or [0])[0] or 0
