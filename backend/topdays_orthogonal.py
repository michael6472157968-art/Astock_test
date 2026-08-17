"""topdays/updays/downdays/lowdays 正交性 vs 反转/低波动/低换手。"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PKL = os.path.join(DATA_DIR, "long_daily.pkl")
EXT = os.path.join(DATA_DIR, "factor_pro_ext.pkl")
BASIC = os.path.join(DATA_DIR, "daily_basic.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def ts_std(s, d):
    return s.groupby(level="ts_code").rolling(d).std().reset_index(level=0, drop=True)


def main():
    ext = pd.read_pickle(EXT)
    for col in ["updays", "downdays", "lowdays", "topdays", "close"]:
        ext[col] = pd.to_numeric(ext[col], errors="coerce")
    ext["trade_date"] = pd.to_datetime(ext["trade_date"].astype(str), errors="coerce")
    ext = ext.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    df = pd.read_pickle(PKL)
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = df["close"]
    ret = c.groupby(level="ts_code").pct_change()

    db = pd.read_pickle(BASIC)
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db["turnover_rate"] = pd.to_numeric(db["turnover_rate"], errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    existing = {
        "反转42d": -c.groupby(level="ts_code").pct_change(42),
        "低波动21d": -ts_std(ret, 21),
        "低换手": -db["turnover_rate"],
    }

    for nname in ["topdays", "updays", "downdays", "lowdays"]:
        a = cs_rank(ext[nname])
        line = f"{nname}: "
        for ename, f in existing.items():
            b = cs_rank(f)
            tmp = pd.DataFrame({"a": a, "b": b}).dropna()
            if len(tmp) < 1000:
                line += f"{ename}=N/A "
                continue
            daily = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
            line += f"{ename}={daily.mean():+.3f} "
        print(line)


if __name__ == "__main__":
    main()
