"""因子引擎 — 诊断 + 自动匹配（配置驱动）。

所有因子从 data/factor_meta.json 读配置，按 compute 类型动态计算。
新增因子：改 factor_meta.json（复用已有 compute 类型）或加一个 compute 函数。

诊断语义（非时间序列择时）：计算该股因子值 + 全市场横截面分位 → 给结论。
数据缺失的因子（如 ps_ttm/dv_ttm/财务）降级提示「数据未接入」，后续补数据自动生效。
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.database import async_session
from app.services.factor_meta import load_factors

logger = logging.getLogger("factor_engine")

# daily_basic 表可用字段（已补全 ps_ttm/dv_ttm/pe_ttm/ps/dv_ratio）
_DAILY_BASIC_FIELDS = {"pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm", "total_mv", "circ_mv", "turnover_rate"}

_EXCLUDE = "AND d.ts_code NOT LIKE '%%ST%%' AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '920%%'"


async def _latest_date(sess) -> str:
    r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
    return r.scalar()


async def _stock_latest_date(sess, ts_code: str) -> str | None:
    """该股自己的最新交易日（避免停牌/数据未同步导致全市场最新日无该股数据）。"""
    r = await sess.execute(text(
        "SELECT MAX(trade_date) FROM stock_daily WHERE ts_code = :c"
    ), {"c": ts_code})
    return r.scalar()


async def _nth_prev_date(sess, trade_date: str, n: int) -> str | None:
    """trade_date 往前第 n 个交易日（dates[0]=trade_date, dates[n]=第 n 个）。"""
    r = await sess.execute(text(
        "SELECT trade_date FROM stock_daily WHERE trade_date <= :td "
        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT :lim"
    ), {"td": trade_date, "lim": n + 1})
    dates = [row[0] for row in r.fetchall()]
    return dates[n] if len(dates) > n else None


def _percentile(value: float, universe: list[float]) -> float:
    """value 在 universe 中的百分位（0-100，越低越靠前）。"""
    if not universe:
        return 50.0
    below = sum(1 for v in universe if v <= value)
    return round(below / len(universe) * 100, 1)


async def _universe_return(sess, trade_date: str, prev_date: str) -> list[float]:
    """全市场近 N 日收益分布。"""
    r = await sess.execute(text(f"""
        SELECT (d1.close - d2.close) / NULLIF(d2.close, 0)
        FROM stock_daily d1
        JOIN stock_daily d2 ON d2.ts_code = d1.ts_code AND d2.trade_date = :pd
        WHERE d1.trade_date = :td AND d1.close > 0 AND d2.close > 0 {_EXCLUDE.replace('d.', 'd1.')}
    """), {"td": trade_date, "pd": prev_date})
    return [float(x[0]) for x in r.fetchall() if x[0] is not None]


async def _stock_return(sess, ts_code: str, trade_date: str, prev_date: str) -> float | None:
    r = await sess.execute(text("""
        SELECT (d1.close - d2.close) / NULLIF(d2.close, 0)
        FROM stock_daily d1
        JOIN stock_daily d2 ON d2.ts_code = d1.ts_code AND d2.trade_date = :pd
        WHERE d1.ts_code = :c AND d1.trade_date = :td
    """), {"c": ts_code, "td": trade_date, "pd": prev_date})
    row = r.fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def _stock_vol(sess, ts_code: str, trade_date: str, prev_date: str) -> float | None:
    """近 N 日收益率标准差（低波动因子）。"""
    r = await sess.execute(text("""
        SELECT pct_chg FROM stock_daily
        WHERE ts_code = :c AND trade_date > :pd AND trade_date <= :td AND pct_chg IS NOT NULL
    """), {"c": ts_code, "td": trade_date, "pd": prev_date})
    vals = [float(x[0]) for x in r.fetchall()]
    if len(vals) < 5:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var ** 0.5


async def _universe_vol(sess, trade_date: str, prev_date: str) -> list[float]:
    r = await sess.execute(text(f"""
        SELECT d.ts_code FROM stock_daily d WHERE d.trade_date = :td {_EXCLUDE}
    """), {"td": trade_date})
    codes = [row[0] for row in r.fetchall()]
    vols = []
    for c in codes:
        v = await _stock_vol(sess, c, trade_date, prev_date)
        if v is not None:
            vols.append(v)
    return vols


async def _stock_corr_price_vol(sess, ts_code: str, trade_date: str, prev_date: str) -> float | None:
    """近 N 日 收盘价 与 成交量 的相关性（量价背离：负相关=缩量涨）。"""
    r = await sess.execute(text("""
        SELECT close, volume FROM stock_daily
        WHERE ts_code = :c AND trade_date > :pd AND trade_date <= :td AND volume > 0
        ORDER BY trade_date
    """), {"c": ts_code, "td": trade_date, "pd": prev_date})
    rows = r.fetchall()
    if len(rows) < 10:
        return None
    closes = [float(x[0]) for x in rows]
    vols = [float(x[1]) for x in rows]
    n = len(closes)
    mc, mv = sum(closes) / n, sum(vols) / n
    cov = sum((closes[i] - mc) * (vols[i] - mv) for i in range(n)) / n
    sc = (sum((v - mc) ** 2 for v in closes) / n) ** 0.5
    sv = (sum((v - mv) ** 2 for v in vols) / n) ** 0.5
    return cov / (sc * sv) if sc > 0 and sv > 0 else 0.0


async def _stock_daily_basic(sess, ts_code: str, field: str) -> float | None:
    if field not in _DAILY_BASIC_FIELDS:
        return None
    r = await sess.execute(text(
        f"SELECT {field} FROM daily_basic WHERE ts_code = :c AND {field} IS NOT NULL "
        f"ORDER BY trade_date DESC LIMIT 1"
    ), {"c": ts_code})
    row = r.fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def _universe_daily_basic(sess, field: str) -> list[float]:
    if field not in _DAILY_BASIC_FIELDS:
        return []
    r = await sess.execute(text(f"""
        SELECT d.{field} FROM daily_basic d
        JOIN stocks s ON s.ts_code = d.ts_code
        WHERE d.trade_date = (SELECT MAX(trade_date) FROM daily_basic)
          AND d.{field} IS NOT NULL AND d.{field} > 0
          AND s.name NOT LIKE '%ST%' AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '920%'
    """))
    return [float(x[0]) for x in r.fetchall()]


async def _stock_vol_surge_top(sess, ts_code: str, trade_date: str) -> dict:
    """放量见顶：量比>2 且当日上涨 → 看跌信号。"""
    r = await sess.execute(text("""
        SELECT d.volume, d.pct_chg, av.avg_vol
        FROM stock_daily d
        JOIN (SELECT ts_code, AVG(volume) AS avg_vol FROM stock_daily
              WHERE trade_date <= :td AND trade_date >= :td20 GROUP BY ts_code) av
          ON av.ts_code = d.ts_code
        WHERE d.ts_code = :c AND d.trade_date = :td
    """), {"c": ts_code, "td": trade_date, "td20": trade_date})
    row = r.fetchone()
    if not row or not row[2]:
        return {"triggered": False, "vol_ratio": None, "pct_chg": None}
    vol_ratio = float(row[0]) / float(row[2]) if float(row[2]) > 0 else None
    pct = float(row[1]) if row[1] is not None else None
    triggered = vol_ratio is not None and pct is not None and vol_ratio > 2 and pct > 0
    return {"triggered": triggered, "vol_ratio": round(vol_ratio, 2) if vol_ratio else None, "pct_chg": pct}


async def _stock_financial(sess, ts_code: str, field: str) -> float | None:
    """该股最新一期财务指标值（fina_indicator 表）。"""
    if field not in {"cfps_yoy", "ocf_yoy", "ocfps", "ocf_to_debt", "dt_netprofit_yoy", "roe_yoy",
                     "basic_eps_yoy", "roe", "roa", "grossprofit_margin", "netprofit_margin",
                     "or_yoy", "netprofit_yoy", "debt_to_assets"}:
        return None
    r = await sess.execute(text(
        f"SELECT {field} FROM fina_indicator WHERE ts_code = :c AND {field} IS NOT NULL "
        f"ORDER BY end_date DESC LIMIT 1"
    ), {"c": ts_code})
    row = r.fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def _universe_financial(sess, field: str) -> list[float]:
    """全市场最新一期财务指标分布（每股取最新 end_date）。"""
    if field not in {"cfps_yoy", "ocf_yoy", "ocfps", "ocf_to_debt", "dt_netprofit_yoy", "roe_yoy",
                     "basic_eps_yoy", "roe", "roa", "grossprofit_margin", "netprofit_margin",
                     "or_yoy", "netprofit_yoy", "debt_to_assets"}:
        return []
    r = await sess.execute(text(f"""
        SELECT fi.{field} FROM fina_indicator fi
        JOIN (SELECT ts_code, MAX(end_date) AS max_ed FROM fina_indicator GROUP BY ts_code) t
          ON t.ts_code = fi.ts_code AND t.max_ed = fi.end_date
        WHERE fi.{field} IS NOT NULL
    """))
    return [float(x[0]) for x in r.fetchall()]


async def diagnose(ts_code: str, factor_id: str) -> dict:
    """因子诊断：该股因子值 + 全市场分位 + 结论。"""
    factors = load_factors()
    meta = factors.get(factor_id)
    if not meta:
        return {"error": f"未知因子 {factor_id}"}

    compute = meta["compute"]
    ctype = compute["type"]
    direction = compute.get("direction", "high_good")

    async with async_session() as sess:
        td = await _stock_latest_date(sess, ts_code)
        if not td:
            return {"error": "该股无行情数据"}

        value = None
        percentile = None
        value_desc = ""

        if ctype == "return":
            w = compute["window"]
            pd = await _nth_prev_date(sess, td, w)
            if not pd:
                return {"error": "历史数据不足"}
            value = await _stock_return(sess, ts_code, td, pd)
            uni = await _universe_return(sess, td, pd)
            if value is not None:
                percentile = _percentile(value, uni)
                value_desc = f"近{w}日收益 {value*100:+.1f}%"

        elif ctype == "vol":
            w = compute["window"]
            pd = await _nth_prev_date(sess, td, w)
            if not pd:
                return {"error": "历史数据不足"}
            value = await _stock_vol(sess, ts_code, td, pd)
            uni = await _universe_vol(sess, td, pd)
            if value is not None:
                percentile = _percentile(value, uni)
                value_desc = f"近{w}日波动率 {value:.2f}%"

        elif ctype == "corr_price_vol":
            w = compute["window"]
            pd = await _nth_prev_date(sess, td, w)
            if not pd:
                return {"error": "历史数据不足"}
            value = await _stock_corr_price_vol(sess, ts_code, td, pd)
            if value is not None:
                # 量价背离：负相关=背离(好)，按符号映射分位（全市场价量相关计算量大，用符号简化）
                percentile = 85 if value > 0.3 else (60 if value > 0 else (30 if value > -0.3 else 10))
                value_desc = f"近{w}日价量相关 {value:+.3f}"

        elif ctype == "daily_basic_field":
            field = compute["field"]
            value = await _stock_daily_basic(sess, ts_code, field)
            if value is None:
                return {"error": f"字段 {field} 数据未接入"}
            uni = await _universe_daily_basic(sess, field)
            percentile = _percentile(value, uni)
            value_desc = f"{field}={value:.3f}"

        elif ctype == "financial_field":
            field = compute["field"]
            value = await _stock_financial(sess, ts_code, field)
            if value is None:
                return {"error": f"财务字段 {field} 数据未接入"}
            uni = await _universe_financial(sess, field)
            percentile = _percentile(value, uni)
            value_desc = f"{field}={value:.2f}"

        elif ctype == "vol_surge_top":
            surge = await _stock_vol_surge_top(sess, ts_code, td)
            value_desc = (
                f"量比{surge['vol_ratio']}，当日{surge['pct_chg']:+.1f}%"
                if surge["vol_ratio"] is not None and surge["pct_chg"] is not None
                else "量比/涨跌数据不足"
            )
            return {
                "factor_id": factor_id, "factor_code": meta["code"], "factor_name": meta["name"],
                "value_desc": value_desc,
                "market_percentile": None,
                "triggered": surge["triggered"],
                "conclusion": (
                    "⚠ 触发放量见顶信号：量比>2 且上涨，未来看跌(看跌准确率+13.9pp)，建议减仓/回避。"
                    if surge["triggered"] else
                    "未触发放量见顶信号，无看跌预警。"
                ),
                "direction": direction,
            }

        else:
            return {"error": f"未知计算类型 {ctype}"}

        if value is None:
            return {"error": "该股数据不足"}

    # 结论
    if direction == "low_good":
        if percentile is not None and percentile < 20:
            concl = f"{value_desc}，处于全市场最低 {percentile}%（低分位），符合「{meta['name']}」买入特征。{meta['perf']}。"
            strength = "强"
        elif percentile is not None and percentile > 80:
            concl = f"{value_desc}，处于全市场最高 {percentile}%（高分位），与「{meta['name']}」买入方向相反。"
            strength = "反向"
        else:
            concl = f"{value_desc}，处于全市场 {percentile}% 分位，因子信号中性。"
            strength = "中"
    else:  # high_good
        if percentile is not None and percentile > 80:
            concl = f"{value_desc}，处于全市场最高 {percentile}%（高分位），符合「{meta['name']}」买入特征。{meta['perf']}。"
            strength = "强"
        elif percentile is not None and percentile < 20:
            concl = f"{value_desc}，处于全市场最低 {percentile}%（低分位），与「{meta['name']}」买入方向相反。"
            strength = "反向"
        else:
            concl = f"{value_desc}，处于全市场 {percentile}% 分位，因子信号中性。"
            strength = "中"

    return {
        "factor_id": factor_id, "factor_code": meta["code"], "factor_name": meta["name"],
        "dim": meta["dim"], "signal": meta["signal"],
        "value_desc": value_desc, "market_percentile": percentile,
        "conclusion": concl, "strength": strength, "direction": direction,
    }


async def match(ts_code: str) -> dict:
    """因子自动匹配：遍历各因子，推荐该股当前最突出的因子特征。"""
    factors = load_factors()
    # 只匹配有 DB 数据可算的因子（排除财务/未接入字段）
    candidates = []
    for fid, meta in factors.items():
        ctype = meta["compute"]["type"]
        if ctype in ("financial_field",):
            continue
        if ctype == "daily_basic_field" and meta["compute"]["field"] not in _DAILY_BASIC_FIELDS:
            continue
        try:
            d = await diagnose(ts_code, fid)
        except Exception as e:
            logger.warning(f"match {fid} failed: {e}")
            continue
        if "error" in d or d.get("market_percentile") is None:
            continue
        candidates.append(d)

    if not candidates:
        return {"stock_code": ts_code, "matches": [], "note": "无可用因子数据"}

    # 找「强」信号因子，其次「反向」警示
    strong = [c for c in candidates if c.get("strength") == "强"]
    picks = strong if strong else candidates[:3]

    matches = [{
        "factor_id": c["factor_id"], "factor_code": c["factor_code"],
        "factor_name": c["factor_name"], "strength": c["strength"],
        "value_desc": c["value_desc"], "percentile": c["market_percentile"],
        "reason": c["conclusion"],
    } for c in picks[:3]]

    return {"stock_code": ts_code, "matches": matches}


async def diagnose_all(ts_code: str) -> dict:
    """全因子诊断：遍历因子库所有因子，算全市场分位 + 等权综合得分。

    供诊股页使用：每只股票返回所有因子的分位 + IC + 综合得分(等权)。
    因子库更新(factor_meta.json)后自动用新因子集，无需改此函数（配置驱动）。
    """
    factors = load_factors()
    items: list[dict] = []
    goodness_sum = 0.0
    n_valid = 0

    for fid, meta in factors.items():
        try:
            d = await diagnose(ts_code, fid)
        except Exception as e:
            logger.warning(f"diagnose_all {fid} failed: {e}")
            continue

        item = {
            "factor_id": fid,
            "code": meta["code"],
            "name": meta["name"],
            "dim": meta["dim"],
            "ic": meta.get("ic"),
            "signal": meta.get("signal"),
        }
        if "error" in d:
            item["percentile"] = None
            item["note"] = d["error"]
            items.append(item)
            continue

        pct = d.get("market_percentile")
        direction = d.get("direction")
        item["percentile"] = pct
        item["strength"] = d.get("strength")
        item["value_desc"] = d.get("value_desc")
        item["conclusion"] = d.get("conclusion")

        if pct is not None:
            # 等权综合：low_good 低分位=好(100-pct)，high_good 高分位=好(pct)
            goodness = pct if direction == "high_good" else (100 - pct)
            item["goodness"] = round(goodness, 1)
            goodness_sum += goodness
            n_valid += 1

        items.append(item)

    composite = round(goodness_sum / n_valid, 1) if n_valid else None
    return {
        "ts_code": ts_code,
        "composite_score": composite,
        "factor_count": len(items),
        "valid_count": n_valid,
        "factors": items,
    }
