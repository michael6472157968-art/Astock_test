"""concentration(筹码宽度) 精筛：行业+市值中性化检验。

复用 neutralization_test.py 框架。判断 concentration 是不是真 alpha 还是市值/行业 beta。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")
DB_PATH = os.path.join(DATA_DIR, "stock_analyzer.db")


def main(fwd: int):
    cq = pd.read_pickle(CYQ_PKL)
    df = pd.read_pickle(LONG_PKL)
    db = pd.read_pickle(BASIC_PKL)

    for col in ["cost_5pct", "cost_95pct", "weight_avg"]:
        cq[col] = pd.to_numeric(cq[col], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    cq = cq.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()
    conc = (cq["cost_95pct"] - cq["cost_5pct"]) / cq["weight_avg"].replace(0, np.nan)

    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = df["close"]

    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db["total_mv"] = pd.to_numeric(db["total_mv"], errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    conn = sqlite3.connect(DB_PATH)
    ind_map = pd.read_sql("SELECT ts_code, industry FROM stocks", conn).set_index("ts_code")["industry"]
    conn.close()

    log_mv = np.log(db["total_mv"].replace(0, np.nan))
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1

    panel = pd.DataFrame({"f": conc, "lm": log_mv, "r": fwd_ret}).dropna()
    ind = ind_map.reindex(panel.index.get_level_values("ts_code")).values
    panel["ind"] = ind
    panel = panel.dropna(subset=["ind"])
    if len(panel) < 5000:
        print("样本不足")
        return

    def ic_of(series):
        fr = series.groupby(level="trade_date").rank(pct=True)
        rr = panel["r"].groupby(level="trade_date").rank(pct=True)
        tmp = pd.DataFrame({"a": fr, "b": rr}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"])).dropna()
        return ic.mean(), ic.mean() / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0

    raw_ic, raw_t = ic_of(panel["f"])

    fz = panel["f"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
    panel["fz"] = fz
    f_ind = fz - panel.groupby(["ind", "trade_date"])["fz"].transform("mean")
    lm_z = panel["lm"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
    beta = (f_ind * lm_z).groupby(level="trade_date").transform("sum") / (lm_z ** 2).groupby(level="trade_date").transform("sum")
    resid = f_ind - beta * lm_z
    panel["resid"] = resid
    neu_ic, neu_t = ic_of(panel["resid"])

    print(f"concentration 前向{fwd}日: 原始IC={raw_ic:+.4f} t={raw_t:+.1f} | 中性IC={neu_ic:+.4f} t={neu_t:+.1f} | 变化={neu_ic - raw_ic:+.4f}")
    print(f"  -> {'✅ 真alpha(中性后仍显著)' if abs(neu_t) > 5 else '⚠️ 市值/行业beta占比高'}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
