"""concentration(筹码宽度) 正交性检验 vs 现有因子。

concentration = (cost_95pct - cost_5pct) / weight_avg，IC +0.038(fwd20)。
判断它是否独立于反转/低波动/低换手/价值/获利盘。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def ts_std(s, d):
    return s.groupby(level="ts_code").rolling(d).std().reset_index(level=0, drop=True)


def main():
    cq = pd.read_pickle(CYQ_PKL)
    df = pd.read_pickle(LONG_PKL)
    db = pd.read_pickle(BASIC_PKL)

    for col in ["winner_rate", "cost_5pct", "cost_95pct", "weight_avg"]:
        cq[col] = pd.to_numeric(cq[col], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    cq = cq.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = df["close"]
    ret = c.groupby(level="ts_code").pct_change()

    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    for col in ["pb", "turnover_rate"]:
        db[col] = pd.to_numeric(db[col], errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    conc = (cq["cost_95pct"] - cq["cost_5pct"]) / cq["weight_avg"].replace(0, np.nan)

    existing = {
        "反转42d": -c.groupby(level="ts_code").pct_change(42),
        "低波动21d": -ts_std(ret, 21),
        "低换手": -db["turnover_rate"],
        "价值BP": 1.0 / db["pb"].replace(0, np.nan),
        "获利盘winner_rate": cq["winner_rate"],
    }

    print("=== concentration(筹码宽度) vs 现有因子 时间平均横截面Spearman ===\n")
    a = cs_rank(conc)
    for name, f in existing.items():
        b = cs_rank(f)
        tmp = pd.DataFrame({"a": a, "b": b}).dropna()
        if len(tmp) < 1000:
            print(f"  {name:<20} 样本不足")
            continue
        daily = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
        mark = "  <- 高相关" if abs(daily.mean()) > 0.4 else ("  <- 中相关" if abs(daily.mean()) > 0.2 else "")
        print(f"  {name:<20} corr={daily.mean():+.3f}{mark}")


if __name__ == "__main__":
    main()
