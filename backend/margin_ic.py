"""融资融券(杠杆资金)因子横截面 IC 检验。

因子：
- rzye_chg(融资余额变化率): 融资余额日 pct_change，增=杠杆进场
- rzmre_ratio(融资买入占比): 融资买入额/成交额，活跃度
- rzye_mv(融资余额/流通市值): 杠杆程度
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stock_analyzer.db")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    conn = sqlite3.connect(DB_PATH)
    mr = pd.read_sql("SELECT trade_date, ts_code, rzye, rzmre, rqye FROM margin_records", conn)
    sd = pd.read_sql("SELECT trade_date, ts_code, close, amount FROM stock_daily", conn)
    db = pd.read_sql("SELECT trade_date, ts_code, circ_mv FROM daily_basic", conn)
    conn.close()

    for df in [mr, sd, db]:
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")

    mr = mr.sort_values(["ts_code", "trade_date"])
    mr["rzye_chg"] = mr.groupby("ts_code")["rzye"].pct_change()

    m = mr.merge(sd[["trade_date", "ts_code", "close", "amount"]], on=["trade_date", "ts_code"], how="left")
    m = m.merge(db[["trade_date", "ts_code", "circ_mv"]], on=["trade_date", "ts_code"], how="left")
    m = m.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])

    m["rzmre_ratio"] = m["rzmre"] / m["amount"].replace(0, np.nan)
    m["rzye_mv"] = m["rzye"] / m["circ_mv"].replace(0, np.nan)

    c = m["close"]
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    F = {
        "融资余额变化率(rzye_chg)": m["rzye_chg"],
        "融资买入占比(rzmre_ratio)": m["rzmre_ratio"],
        "融资余额/流通市值(rzye_mv)": m["rzye_mv"],
    }

    print(f"\n=== 融资融券因子横截面 IC (fwd={fwd}) ===")
    for name, f in F.items():
        tmp = pd.DataFrame({"f": cs_rank(f), "r": r_rank}).dropna()
        if len(tmp) < 5000:
            print(f"  {name}: 样本不足 {len(tmp)}")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"])).dropna()
        mean_ic = ic.mean()
        t = mean_ic / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0
        icir = mean_ic / ic.std() if ic.std() > 0 else 0.0
        print(f"  {name:<28} mean IC={mean_ic:+.4f}  t={t:+.2f}  ICIR={icir:+.3f}  天数={len(ic)}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(fwd)
