"""龙虎榜事件研究：上榜股票 vs 全市场超额收益。

龙虎榜是事件驱动（异动上榜），适合作事件研究（上榜后整体涨/跌）而非横截面排序。
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


def main(fwd: int):
    conn = sqlite3.connect(DB_PATH)
    tl = pd.read_sql("SELECT trade_date, ts_code, net_amount, pct_change FROM top_list", conn)
    ti = pd.read_sql("SELECT trade_date, ts_code, exalter, net_buy FROM top_inst", conn)
    conn.close()
    tl["trade_date"] = pd.to_datetime(tl["trade_date"].astype(str), errors="coerce")
    ti["trade_date"] = pd.to_datetime(ti["trade_date"].astype(str), errors="coerce")

    is_inst = ti["exalter"].astype(str).str.contains("机构|基金|QFII|社保|专用", na=False)
    inst_net = ti[is_inst].groupby(["trade_date", "ts_code"])["net_buy"].sum().reset_index().rename(columns={"net_buy": "inst_net"})
    tl = tl.merge(inst_net, on=["trade_date", "ts_code"], how="left")

    df = pd.read_pickle(LONG_PKL)
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[["ts_code", "trade_date", "close"]].dropna().sort_values(["ts_code", "trade_date"])
    df["fwd_ret"] = df.groupby("ts_code")["close"].shift(-fwd) / df["close"] - 1

    market = df.groupby("trade_date")["fwd_ret"].mean().reset_index().rename(columns={"fwd_ret": "market_ret"})

    m = tl.merge(df, on=["trade_date", "ts_code"], how="left")
    m = m.dropna(subset=["fwd_ret"])
    m = m.merge(market, on="trade_date", how="left")
    m["excess"] = m["fwd_ret"] - m["market_ret"]

    print(f"\n=== 龙虎榜事件研究 (fwd={fwd}) ===")
    print(f"  上榜股票样本: {len(m)} 条")
    print(f"  上榜股票平均 fwd 收益: {m['fwd_ret'].mean()*100:+.2f}%")
    print(f"  上榜股票平均超额: {m['excess'].mean()*100:+.3f}%  (t={m['excess'].mean()/m['excess'].std()*np.sqrt(len(m)):+.2f})")

    # 机构净买入分组
    m_inst = m.dropna(subset=["inst_net"])
    pos = m_inst[m_inst["inst_net"] > 0]["excess"]
    neg = m_inst[m_inst["inst_net"] <= 0]["excess"]
    if len(pos) > 50 and len(neg) > 50:
        print(f"\n  机构净买入>0: 超额 {pos.mean()*100:+.3f}% (n={len(pos)})")
        print(f"  机构净买入<=0: 超额 {neg.mean()*100:+.3f}% (n={len(neg)})")
        print(f"  机构净买入>0 vs <=0 超额差: {(pos.mean()-neg.mean())*100:+.3f}%")

    # 当日大涨(涨停/大阳) vs 大跌 上榜的超额
    up = m[m["pct_change"] > 5]["excess"]
    dn = m[m["pct_change"] < -5]["excess"]
    if len(up) > 50 and len(dn) > 50:
        print(f"\n  当日涨>5%上榜: 超额 {up.mean()*100:+.3f}% (n={len(up)})")
        print(f"  当日跌<-5%上榜: 超额 {dn.mean()*100:+.3f}% (n={len(dn)})")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(fwd)
