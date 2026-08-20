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


# ── 第0步 大盘分析 ──

async def step0_market(td: str) -> dict:
    """大盘分析：三大指数 + 涨跌家数 + 市场温度。"""
    async with async_session() as sess:
        # 指数涨跌
        r = await sess.execute(text("""
            SELECT ts_code, pct_chg FROM stock_daily
            WHERE trade_date = :td AND ts_code IN ('000001.SH','399001.SZ','399006.SZ','000688.SH')
        """), {"td": td})
        indices = {row[0]: round(float(row[1]), 2) if row[1] is not None else 0 for row in r.fetchall()}

        # 涨跌家数（排除指数）
        r2 = await sess.execute(text("""
            SELECT SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM stock_daily WHERE trade_date = :td
              AND ts_code NOT IN ('000001.SH','399001.SZ','399006.SZ','000688.SH')
        """), {"td": td})
        row = r2.fetchone()
        up = int(row[0] or 0) if row else 0
        down = int(row[1] or 0) if row else 0
        flat = int(row[2] or 0) if row else 0
        total = int(row[3] or 0) if row else 0
        up_ratio = round(up / total * 100, 1) if total else 0

    if up_ratio >= 70:
        temp = "热"
    elif up_ratio >= 50:
        temp = "暖"
    elif up_ratio >= 30:
        temp = "中性"
    else:
        temp = "冷"

    return {
        "indices": indices, "up": up, "down": down, "flat": flat, "total": total,
        "up_ratio": up_ratio, "temp": temp,
    }


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

    # 情绪周期：上升/震荡/退潮（决定能不能出手、追多高）
    if zha_rate < 15 and (avg_premium is None or avg_premium > 0):
        cycle = "上升期"
    elif zha_rate > 30 or (avg_premium is not None and avg_premium < 0):
        cycle = "退潮期"
    else:
        cycle = "震荡期"

    return {
        "up": up, "down": down, "zha": zha, "zha_rate": zha_rate,
        "avg_premium": avg_premium, "prem_pos": prem_pos, "prev_date": prev,
        "rongcuo": rongcuo, "cycle": cycle,
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


# ── 第6步 观察风向标（连板股，非买入推荐）──

async def step6_focus(td: str) -> list:
    """连板股作为观察接力的风向标，不是买入推荐。

    买不买取决于第7步的操作风格判断(情绪周期+晋级率)，这里只列出标的供观察。
    """
    async with async_session() as sess:
        # 连板股（≥2板），作为接力情绪风向标
        r = await sess.execute(text("""
            SELECT l.name, l.status, s.industry FROM limit_list_records l
            JOIN stocks s ON s.ts_code = l.ts_code
            WHERE l.trade_date = :td AND l.limit_type = 'U' AND l.status != ''
              AND CAST(l.status AS REAL) >= 2
            ORDER BY CAST(l.status AS REAL) DESC, l.name LIMIT 8
        """), {"td": td})
        focus = [{"name": row[0], "board": row[1], "industry": row[2]} for row in r.fetchall()]
    return focus


# ── 第7步 操作风格 + 明日计划 ──

# 历史回测证据(backtest_emotion_cycle.py 输出，基于 20240812~20260819 共 490 交易日)
# 用途：给「操作风格」标注历史胜率，让结论有数据背书而非拍脑袋。
# 追高标=当日连板股(≥2板)次日均涨；做低位=首板股次日均涨；大面股=昨涨停今跌>3%的日均只数。
# 数据更新后重跑 backtest_emotion_cycle.py 刷新本表。
CYCLE_EVIDENCE = {
    "上升期": {"样本": 33, "追高标": 2.89, "做低位": 1.44, "大面股日均": 10.2},
    "震荡期": {"样本": 249, "追高标": 3.18, "做低位": 1.83, "大面股日均": 12.5},
    "退潮期": {"样本": 195, "追高标": 2.68, "做低位": 1.92, "大面股日均": 25.6},
}


# 三剧本推演(backtest_transition.py 周期转移矩阵输出，基于 490 交易日)
# 今日周期 → 明日三路径(强延续=明日上升 / 弱分歧=明日震荡 / 强退潮=明日退潮)，
# 含历史概率 + 各剧本次日追高标/做低位收益。核心规律：明日退潮时追高标收益腰斩。
# 数据更新后重跑 backtest_transition.py 刷新本表。
CYCLE_SCENARIOS = {
    "上升期": [
        {"剧本": "强延续", "概率": 3.0, "操作": "追高标", "追高标": 5.08, "做低位": 7.67},
        {"剧本": "弱分歧", "概率": 45.5, "操作": "做低位·首板", "追高标": 4.01, "做低位": 2.40},
        {"剧本": "强退潮", "概率": 51.5, "操作": "空仓", "追高标": 1.77, "做低位": 0.23},
    ],
    "震荡期": [
        {"剧本": "强延续", "概率": 6.4, "操作": "追高标", "追高标": 4.11, "做低位": 2.61},
        {"剧本": "弱分歧", "概率": 52.6, "操作": "做低位·首板", "追高标": 3.93, "做低位": 2.53},
        {"剧本": "强退潮", "概率": 41.0, "操作": "空仓", "追高标": 2.08, "做低位": 0.82},
    ],
    "退潮期": [
        {"剧本": "强延续", "概率": 8.2, "操作": "追高标", "追高标": 4.26, "做低位": 3.33},
        {"剧本": "弱分歧", "概率": 52.6, "操作": "做低位·首板", "追高标": 3.52, "做低位": 2.72},
        {"剧本": "强退潮", "概率": 39.3, "操作": "空仓", "追高标": 1.22, "做低位": 0.54},
    ],
}


def step7_plan(steps: dict) -> dict:
    e = steps.get("emotion", {})
    l = steps.get("ladder", {})
    s = steps.get("sectors", {})
    loss = steps.get("loss", {})

    cycle = e.get("cycle", "震荡期")
    j1 = l.get("j1")

    # 操作风格判断：情绪周期 + 晋级率(1进2) 推导，不是无脑推荐龙头。
    # 回测修正(见 CYCLE_EVIDENCE)：追高标超额随周期递增(退潮+0.77%→震荡+1.35%→上升+1.44%)，
    # 震荡期追高标绝对收益最高(+3.18%)且次日<0占比最低(12%)，故震荡期可追高标；
    # 退潮期追高标平均仍正(+2.68%)但大面股日均25.6只(2.5倍)，空仓依据是「方差大易踩雷」而非「平均会亏」。
    if cycle == "上升期":
        if j1 is not None and j1 >= 30:
            style = "接力有肉，可追 2-3 板主线龙头"
        elif j1 is not None and j1 < 20:
            style = "接力亏钱，只做首板/1进2，不追高标"
        else:
            style = "做主线首板 / 2 板"
    elif cycle == "震荡期":
        style = "可追主线龙头（震荡期追高标容错率最高）"
    else:
        style = "空仓观望；或只做首板（大面股多，追高标易踩雷）"

    # 规避清单（有依据）
    avoid = []
    if loss.get("big_face") and len(loss["big_face"]) >= 3:
        avoid.append(f"大面股 {len(loss['big_face'])} 只，接力环境差，规避高位连板")
    if cycle == "退潮期":
        avoid.append("退潮期，高标天地板风险大")

    # 明日方向
    if cycle == "上升期":
        verdict = "可参与"
    elif cycle == "退潮期":
        verdict = "规避"
    else:
        verdict = "观望"

    # 具体操作建议
    actions = [f"情绪：{cycle}，容错率{e.get('rongcuo', '')}"]
    actions.append(f"操作：{style}")
    if s.get("strong_sectors"):
        names = "、".join(n for n, _ in s["strong_sectors"][:3])
        actions.append(f"聚焦板块：{names}")
    if l.get("max_board", 0) >= 4:
        actions.append(f"高度：最高{l['max_board']}板，题材有高度")
    else:
        actions.append(f"高度：最高{l.get('max_board', 0)}板，题材高度不足")

    return {
        "cycle": cycle, "style": style,
        "verdict": verdict, "actions": actions, "avoid": avoid,
        "evidence": CYCLE_EVIDENCE.get(cycle),
        "scenarios": CYCLE_SCENARIOS.get(cycle, []),
    }


# ── 选股池次日收益 ──

async def pool_performance(td: str = "") -> dict:
    """选股池次日收益：昨天选的股票，今天(最新完整交易日)的平均涨幅。"""
    latest = td or await _latest_date()
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


async def pool_today(td: str = "") -> dict:
    """选股池今日名单：最新 calc_date 的短线/长线池 15 只。"""
    import json as _json
    latest = td or await _latest_date()
    async with async_session() as sess:
        pools = []
        for ptype, name in [("factor_short", "短线池"), ("factor_long", "长线池")]:
            r = await sess.execute(text("""
                SELECT p.ts_code, p.stock_name, s.industry, p.market_data_json
                FROM stock_pool_results p
                LEFT JOIN stocks s ON s.ts_code = p.ts_code
                WHERE p.calc_date = :td AND p.pool_type = :pt
                ORDER BY p.rank_in_pool ASC
            """), {"td": latest, "pt": ptype})
            items = []
            for row in r.fetchall():
                try:
                    md = _json.loads(row[3]) if row[3] else {}
                except Exception:
                    md = {}
                items.append({
                    "code": row[0], "name": row[1], "industry": row[2] or "",
                    "score": md.get("score"),
                })
            pools.append({"type": ptype, "name": name, "items": items})
    return {"date": latest, "pools": pools}


# ── 汇总 ──

async def compute_review(td: str = "") -> dict:
    if not td:
        td = await _latest_date()
    if not td:
        return {"date": "", "content": {}}

    market = await step0_market(td)
    emotion = await step1_emotion(td)
    ladder = await step2_ladder(td)
    sectors = await step3_sectors(td)
    fund = await step4_fund(td)
    loss = await step5_loss(td)
    watch = await step6_focus(td)

    steps = {
        "market": market, "emotion": emotion, "ladder": ladder, "sectors": sectors,
        "fund": fund, "loss": loss, "watch": watch,
    }
    steps["plan"] = step7_plan(steps)

    # 选股池：今日名单 + 昨日收益
    steps["pool_today"] = await pool_today(td)
    steps["pool_perf"] = await pool_performance(td)

    return {"date": td, "content": steps}
