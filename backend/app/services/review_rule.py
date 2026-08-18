"""7步纯规则复盘引擎——预判次日，不用AI。

7步(核心「预判次日」非「解释涨跌」)：
1. 整体情绪：涨跌停/炸板率/昨日涨停溢价 → 容错率
2. 连板梯队：最高板/断层/晋级率 → 题材强弱
3. 板块结构：涨停股行业聚合(≥3只=强势板块) + 行业轮动
4. 资金流向：成交额前20涨跌 + 龙虎榜机构/游资
5. 亏钱效应：跌幅榜共性 + 大面股
6. 重点票：5-8只标杆(最高板龙头/板块龙头/大面股)
7. 次日计划：规则综合参与/规避信号

全部确定性计算，每步结论引用具体数字，可复现可回测。
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("review_rule")


async def _latest_date() -> str | None:
    async with async_session() as sess:
        r = await sess.execute(text(
            "SELECT trade_date FROM stock_daily GROUP BY trade_date "
            "HAVING COUNT(*) >= 50 ORDER BY trade_date DESC LIMIT 1"
        ))
        return r.scalar()


async def _prev_date(td: str) -> str | None:
    async with async_session() as sess:
        r = await sess.execute(text(
            "SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < :td"
        ), {"td": td})
        return r.scalar()


# ── 第1步 整体情绪 ──

async def step1_emotion(td: str) -> dict:
    async with async_session() as sess:
        r = await sess.execute(text("""
            SELECT limit_type, COUNT(*) FROM limit_list_records
            WHERE trade_date = :td GROUP BY limit_type
        """), {"td": td})
        cnt = {row[0]: row[1] for row in r.fetchall()}
        up = cnt.get("U", 0)
        down = cnt.get("D", 0)
        zha = cnt.get("Z", 0)
        zha_rate = round(zha / (up + zha) * 100, 1) if (up + zha) else 0

    # 昨日涨停溢价
    prev = await _prev_date(td)
    avg_premium = None
    prem_pos = None
    if prev:
        async with async_session() as sess:
            r = await sess.execute(text("""
                SELECT AVG(d.pct_chg), SUM(CASE WHEN d.pct_chg > 0 THEN 1 ELSE 0 END), COUNT(*)
                FROM limit_list_records l
                JOIN stock_daily d ON d.ts_code = l.ts_code AND d.trade_date = :td
                WHERE l.trade_date = :prev AND l.limit_type = 'U'
            """), {"td": td, "prev": prev})
            row = r.fetchone()
            if row and row[2]:
                avg_premium = round(float(row[0]), 2) if row[0] is not None else None
                prem_pos = round(float(row[1]) / row[2] * 100, 1)

    if zha_rate < 15:
        rongcuo = "高"
    elif zha_rate < 30:
        rongcuo = "中"
    else:
        rongcuo = "低"

    return {
        "up": up, "down": down, "zha": zha, "zha_rate": zha_rate,
        "avg_premium": avg_premium, "prem_pos": prem_pos, "prev_date": prev,
        "rongcuo": rongcuo,
    }


# ── 第2步 连板梯队 ──

async def step2_ladder(td: str) -> dict:
    async with async_session() as sess:
        r = await sess.execute(text("""
            SELECT status, COUNT(*) FROM limit_list_records
            WHERE trade_date = :td AND limit_type = 'U' AND status != ''
            GROUP BY status
        """), {"td": td})
        ladder = {}
        for row in r.fetchall():
            try:
                n = int(float(row[0]))
            except (ValueError, TypeError):
                continue
            ladder[n] = row[1]

    if not ladder:
        return {"ladder": {}, "max_board": 0, "gap": None, "board_desc": "无连板数据"}

    max_board = max(ladder)
    keys = sorted(ladder)
    gaps = [b for a, b in zip(keys, keys[1:]) if b - a > 1]
    gap_desc = f"{gaps[0]}板断层" if gaps else "无断层"

    # 晋级率：2板/1板、3板/2板
    j1 = round(ladder.get(2, 0) / ladder.get(1, 0) * 100, 1) if ladder.get(1) else None
    j2 = round(ladder.get(3, 0) / ladder.get(2, 0) * 100, 1) if ladder.get(2) else None

    board_desc = f"最高{max_board}板" + ("，题材有高度" if max_board >= 4 else "，题材高度不足")
    return {
        "ladder": ladder, "max_board": max_board, "gap": gap_desc,
        "j1": j1, "j2": j2, "board_desc": board_desc,
    }


# ── 第3步 板块结构（涨停股行业聚合 + 行业轮动）──

_L2_CODES: set | None = None


async def _get_l2_codes() -> set:
    """申万二级行业代码（134个，模块级缓存）。"""
    global _L2_CODES
    if _L2_CODES is None:
        from app.services.tushare_client import call_tushare
        try:
            cls = await call_tushare("index_classify", src="SW2021", level="L2")
            _L2_CODES = {r["index_code"] for r in cls.to_dict("records")} if cls is not None and not cls.empty else set()
        except Exception:
            _L2_CODES = set()
    return _L2_CODES


async def step3_sectors(td: str) -> dict:
    async with async_session() as sess:
        # 涨停股按行业聚合
        r = await sess.execute(text("""
            SELECT s.industry, COUNT(*) FROM limit_list_records l
            JOIN stocks s ON s.ts_code = l.ts_code
            WHERE l.trade_date = :td AND l.limit_type = 'U' AND s.industry != ''
            GROUP BY s.industry ORDER BY COUNT(*) DESC
        """), {"td": td})
        sector_cnt = [(row[0], row[1]) for row in r.fetchall()]
        strong = [(n, c) for n, c in sector_cnt if c >= 3]

        # 行业轮动：申万二级近20日累计涨幅（Python 算）
        r2 = await sess.execute(text("""
            SELECT sd.code, sc.name, sd.trade_date, sd.pct_chg
            FROM sector_daily sd
            JOIN sectors sc ON sc.code = sd.code
            WHERE sd.trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM sector_daily ORDER BY trade_date DESC LIMIT 20))
            ORDER BY sd.code, sd.trade_date
        """))
        l2 = await _get_l2_codes()
        cum: dict = {}
        for code, name, _td, pct in r2.fetchall():
            if l2 and code not in l2:
                continue
            cum.setdefault(code, {"name": name, "cum": 1.0})
            if pct is not None:
                cum[code]["cum"] *= (1 + pct / 100)
        rotation = sorted(
            [{"code": c, "name": v["name"], "chg": round((v["cum"] - 1) * 100, 2)} for c, v in cum.items()],
            key=lambda x: -x["chg"],
        )[:6]
        rotation_top = [(r["code"], r["name"], r["chg"]) for r in rotation]

    return {
        "strong_sectors": strong,
        "sector_cnt": sector_cnt[:10],
        "rotation_top": rotation_top,
    }


# ── 第4步 资金流向 ──

async def step4_fund(td: str) -> dict:
    async with async_session() as sess:
        # 成交额前20
        r = await sess.execute(text("""
            SELECT s.name, d.pct_chg, d.amount FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td AND d.amount > 0
            ORDER BY d.amount DESC LIMIT 20
        """), {"td": td})
        top20 = [(row[0], round(float(row[1]), 2) if row[1] is not None else 0) for row in r.fetchall()]
        up_cnt = sum(1 for _, pct in top20 if pct > 0)

        # 龙虎榜机构净买（近10日上榜 + 机构席位）
        r2 = await sess.execute(text("""
            SELECT l.ts_code, l.name, SUM(ti.net_buy) FROM top_list l
            JOIN top_inst ti ON ti.ts_code = l.ts_code AND ti.trade_date = l.trade_date
            WHERE l.trade_date = (SELECT MAX(trade_date) FROM top_list WHERE trade_date <= :td)
              AND (ti.exalter LIKE '%机构%' OR ti.exalter LIKE '%专用%' OR ti.exalter LIKE '%基金%'
                   OR ti.exalter LIKE '%QFII%' OR ti.exalter LIKE '%社保%')
            GROUP BY l.ts_code, l.name
            ORDER BY SUM(ti.net_buy) DESC LIMIT 5
        """), {"td": td})
        inst_buy = [(row[1], round(float(row[2]) / 1e4, 0)) for row in r2.fetchall() if row[2] is not None]

    return {
        "top20_up_ratio": round(up_cnt / len(top20) * 100, 1) if top20 else 0,
        "top20": top20[:10],
        "inst_buy": inst_buy,
    }


# ── 第5步 亏钱效应 ──

async def step5_loss(td: str) -> dict:
    async with async_session() as sess:
        # 跌幅榜前10
        r = await sess.execute(text("""
            SELECT s.name, d.pct_chg, s.industry FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.trade_date = :td AND d.pct_chg < -5
            ORDER BY d.pct_chg ASC LIMIT 10
        """), {"td": td})
        losers = [(row[0], round(float(row[1]), 2), row[2]) for row in r.fetchall()]

        # 大面股：昨日涨停今日跌停/大跌
        prev = await _prev_date(td)
        big_face = []
        if prev:
            r2 = await sess.execute(text("""
                SELECT l.name, d.pct_chg FROM limit_list_records l
                JOIN stock_daily d ON d.ts_code = l.ts_code AND d.trade_date = :td
                WHERE l.trade_date = :prev AND l.limit_type = 'U' AND d.pct_chg < -3
                ORDER BY d.pct_chg ASC LIMIT 8
            """), {"td": td, "prev": prev})
            big_face = [(row[0], round(float(row[1]), 2)) for row in r2.fetchall()]

    return {
        "losers": losers,
        "big_face": big_face,
        "loss_desc": f"跌幅榜{len(losers)}只，大面股{len(big_face)}只" + ("，亏钱效应重" if len(big_face) >= 3 else ""),
    }


# ── 第6步 重点票（标杆）──

async def step6_focus(td: str) -> list:
    async with async_session() as sess:
        # 最高板龙头 + 强势板块龙头
        r = await sess.execute(text("""
            SELECT l.name, l.status, s.industry FROM limit_list_records l
            JOIN stocks s ON s.ts_code = l.ts_code
            WHERE l.trade_date = :td AND l.limit_type = 'U' AND l.status != ''
              AND CAST(l.status AS REAL) >= 2
            ORDER BY CAST(l.status AS REAL) DESC, l.name LIMIT 8
        """), {"td": td})
        focus = [{"name": row[0], "board": row[1], "industry": row[2]} for row in r.fetchall()]
    return focus


# ── 第7步 次日计划（规则综合）──

def step7_plan(steps: dict) -> dict:
    e = steps.get("emotion", {})
    l = steps.get("ladder", {})
    s = steps.get("sectors", {})
    loss = steps.get("loss", {})

    signals = []
    # 参与信号
    if e.get("rongcuo") == "高":
        signals.append("容错率高，可积极参与")
    elif e.get("rongcuo") == "低":
        signals.append("容错率低，控制仓位")
    if e.get("avg_premium") is not None and e["avg_premium"] > 0:
        signals.append(f"昨日涨停溢价{e['avg_premium']:+.2f}%，打板有肉")
    elif e.get("avg_premium") is not None and e["avg_premium"] < 0:
        signals.append(f"昨日涨停溢价{e['avg_premium']:+.2f}%，打板亏钱")
    if l.get("max_board", 0) >= 4:
        signals.append("题材有高度，可做高位接力")
    else:
        signals.append("题材高度不足，聚焦低位首板")
    if s.get("strong_sectors"):
        names = "、".join(n for n, _ in s["strong_sectors"][:3])
        signals.append(f"强势板块：{names}")
    if loss.get("big_face") and len(loss["big_face"]) >= 3:
        signals.append("大面股≥3只，亏钱效应重，警惕高位股")

    # 综合判断
    zha_rate = e.get("zha_rate", 50)
    premium = e.get("avg_premium")
    max_board = l.get("max_board", 0)
    if zha_rate < 15 and (premium is None or premium > 0) and max_board >= 3:
        verdict = "可参与"
    elif zha_rate > 30 or (premium is not None and premium < 0):
        verdict = "规避"
    else:
        verdict = "观望"

    return {"signals": signals, "verdict": verdict}


# ── 选股池次日收益 ──

async def pool_performance() -> dict:
    """选股池次日收益：昨天选的股票，今天(最新完整交易日)的平均涨幅。"""
    latest = await _latest_date()
    prev = await _prev_date(latest)
    if not latest or not prev:
        return {"date": latest, "prev_date": prev, "pools": []}

    async with async_session() as sess:
        pools = []
        for ptype, name in [("factor_short", "短线选股"), ("factor_long", "长线选股")]:
            r = await sess.execute(text("""
                SELECT AVG(d.pct_chg),
                       SUM(CASE WHEN d.pct_chg > 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN d.pct_chg < 0 THEN 1 ELSE 0 END),
                       COUNT(*)
                FROM stock_pool_results p
                JOIN stock_daily d ON d.ts_code = p.ts_code AND d.trade_date = :latest
                WHERE p.calc_date = :prev AND p.pool_type = :pt
            """), {"latest": latest, "prev": prev, "pt": ptype})
            row = r.fetchone()
            avg_pct = round(float(row[0]), 2) if row and row[0] is not None else None
            up = int(row[1] or 0) if row else 0
            down = int(row[2] or 0) if row else 0
            total = int(row[3] or 0) if row else 0
            pools.append({
                "type": ptype, "name": name,
                "avg_pct": avg_pct, "up_count": up, "down_count": down, "total": total,
            })

    return {"date": latest, "prev_date": prev, "pools": pools}


# ── 汇总 ──

async def compute_review(td: str = "") -> dict:
    if not td:
        td = await _latest_date()
    if not td:
        return {"date": "", "content": {}}

    emotion = await step1_emotion(td)
    ladder = await step2_ladder(td)
    sectors = await step3_sectors(td)
    fund = await step4_fund(td)
    loss = await step5_loss(td)
    focus = await step6_focus(td)

    steps = {
        "emotion": emotion, "ladder": ladder, "sectors": sectors,
        "fund": fund, "loss": loss, "focus": focus,
    }
    steps["plan"] = step7_plan(steps)

    return {"date": td, "content": steps}
