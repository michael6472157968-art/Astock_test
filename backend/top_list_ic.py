"""龙虎榜因子横截面 IC 检验。

因子：
- inst_net(机构净买入): top_inst 中 exalter 含"机构/基金/专用"的 net_buy 聚合
- yz_net(游资净买入): 非机构席位的 net_buy 聚合
- lhb_net(龙虎榜净买入): top_list.net_amount
- lhb_net_rate(净买入占比): top_list.net_rate

横截面 IC：每天上榜股票内部排序 vs 未来 fwd 日收益。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
DB_PATH = os.path.join(DATA_DIR, "stock_analyzer.db")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    conn = sqlite3.connect(DB_PATH)
    tl = pd.read_sql("SELECT trade_date, ts_code, net_amount, net_rate, l_buy, l_sell FROM top_list", conn)
    ti = pd.read_sql("SELECT trade_date, ts_code, exalter, net_buy FROM top_inst", conn)
    conn.close()

    tl["trade_date"] = pd.to_datetime(tl["trade_date"].astype(str), errors="coerce")
    ti["trade_date"] = pd.to_datetime(ti["trade_date"].astype(str), errors="coerce")

    is_inst = ti["exalter"].astype(str).str.contains("机构|基金|QFII|社保|专用", na=False)
    inst_net = ti[is_inst].groupby(["trade_date", "ts_code"])["net_buy"].sum().reset_index().rename(columns={"net_buy": "inst_net"})
    yz_net = ti[~is_inst].groupby(["trade_date", "ts_code"])["net_buy"].sum().reset_index().rename(columns={"net_buy": "yz_net"})

    df = pd.read_pickle(LONG_PKL)
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[["ts_code", "trade_date", "close"]].dropna()

    m = tl.merge(inst_net, on=["trade_date", "ts_code"], how="left")
    m = m.merge(yz_net, on=["trade_date", "ts_code"], how="left")
    m = m.merge(df, on=["trade_date", "ts_code"], how="left")
    m = m.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])

    c = m["close"]
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    F = {
        "机构净买入(inst_net)": m["inst_net"],
        "游资净买入(yz_net)": m["yz_net"],
        "龙虎榜净买入(net_amount)": m["net_amount"],
        "净买入占比(net_rate)": m["net_rate"],
    }

    print(f"\n=== 龙虎榜因子横截面 IC (上榜股票内部, fwd={fwd}) ===")
    for name, f in F.items():
        tmp = pd.DataFrame({"f": cs_rank(f), "r": r_rank}).dropna()
        if len(tmp) < 1000:
            print(f"  {name}: 样本不足 {len(tmp)}")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"])).dropna()
        mean_ic = ic.mean()
        t = mean_ic / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0
        icir = mean_ic / ic.std() if ic.std() > 0 else 0.0
        print(f"  {name:<24} mean IC={mean_ic:+.4f}  t={t:+.2f}  ICIR={icir:+.3f}  天数={len(ic)}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(fwd)
