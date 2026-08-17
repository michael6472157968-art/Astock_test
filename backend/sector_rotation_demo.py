"""行业轮动分析 demo：验证申万二级行业轮动数据 + 轮动位置判断。"""
import asyncio
import sqlite3

from app.services.tushare_client import call_tushare


async def main():
    # 1. 拉申万二级行业分类（134个）
    cls = await call_tushare("index_classify", src="SW2021", level="L2")
    l2 = {r["index_code"]: r["industry_name"] for r in cls.to_dict("records")}
    print(f"二级行业 {len(l2)} 个")

    # 2. 从 sector_daily 查这些行业最近20日涨幅
    db = sqlite3.connect("data/stock_analyzer.db")
    c = db.cursor()
    # 拿最近20个交易日
    c.execute("SELECT DISTINCT trade_date FROM sector_daily ORDER BY trade_date DESC LIMIT 20")
    dates = sorted([r[0] for r in c.fetchall()])
    d5 = dates[-5:]   # 最近5日
    d20 = dates        # 最近20日

    # 3. 算每个二级行业的5日/20日累计涨幅
    rows = []
    for code, name in l2.items():
        # 20日累计涨幅（首日到末日）
        c.execute("SELECT pct_chg FROM sector_daily WHERE code=? AND trade_date IN (%s) ORDER BY trade_date" % ",".join("?" * len(d20)), [code] + d20)
        pcts = [r[0] for r in c.fetchall() if r[0] is not None]
        if len(pcts) < 5:
            continue
        cum20 = 1.0
        for p in pcts:
            cum20 *= (1 + p / 100)
        cum20 = (cum20 - 1) * 100
        # 5日累计涨幅
        c.execute("SELECT pct_chg FROM sector_daily WHERE code=? AND trade_date IN (%s) ORDER BY trade_date" % ",".join("?" * len(d5)), [code] + d5)
        pcts5 = [r[0] for r in c.fetchall() if r[0] is not None]
        cum5 = 1.0
        for p in pcts5:
            cum5 *= (1 + p / 100)
        cum5 = (cum5 - 1) * 100
        rows.append((name, round(cum5, 2), round(cum20, 2)))
    db.close()

    # 4. 按20日涨幅排序，展示轮动
    rows.sort(key=lambda x: -x[2])
    print(f"\n{'行业':<14}{'5日涨%':<10}{'20日涨%':<10}轮动位置")
    for name, c5, c20 in rows[:12]:
        # 轮动位置：5日强+20日强=主升；20日强+5日转弱=见顶；5日强+20日一般=刚启动
        if c20 > 10 and c5 > 0:
            pos = "主升"
        elif c20 > 10 and c5 < 0:
            pos = "见顶/回调"
        elif c5 > 3 and c20 < 10:
            pos = "刚启动"
        else:
            pos = "震荡"
        print(f"{name:<14}{c5:<10}{c20:<10}{pos}")


if __name__ == "__main__":
    asyncio.run(main())
