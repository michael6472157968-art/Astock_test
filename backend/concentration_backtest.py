"""concentration(筹码宽度) 多空回测 — top20% vs bottom20%，持有N天。

concentration 方向 high_good（筹码宽→涨），做多 q4(最宽) 空 q0(最窄)。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(hold_days: int):
    cq = pd.read_pickle(CYQ_PKL)
    df = pd.read_pickle(LONG_PKL)

    for col in ["cost_5pct", "cost_95pct", "weight_avg"]:
        cq[col] = pd.to_numeric(cq[col], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    cq = cq.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()
    conc = (cq["cost_95pct"] - cq["cost_5pct"]) / cq["weight_avg"].replace(0, np.nan)

    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = df["close"]

    f = cs_rank(conc)  # high_good：筹码宽 = rank高 = 好
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    year = [t.year for t in df.index.get_level_values("trade_date")]

    m = pd.DataFrame({"score": f, "r": fwd_hold, "year": year}).dropna()
    m["q"] = m.groupby(level="trade_date")["score"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"]).astype({"q": int})

    dates = sorted(m.index.get_level_values("trade_date").unique())
    periods_per_year = 252 / hold_days
    rows = []
    for offset in range(hold_days):
        rebal_dates = dates[offset::hold_days]
        for rd in rebal_dates:
            try:
                day = m.xs(rd, level="trade_date")
            except KeyError:
                continue
            q0 = day[day["q"] == 0]
            q4 = day[day["q"] == 4]
            if len(q0) < 3 or len(q4) < 3:
                continue
            ls = q4["r"].mean() - q0["r"].mean()
            rows.append((day["year"].iloc[0], ls))

    r = pd.DataFrame(rows, columns=["year", "ls"])
    gross = r["ls"].mean() * periods_per_year
    sharpe = r["ls"].mean() / r["ls"].std() * np.sqrt(periods_per_year) if r["ls"].std() > 0 else 0
    pos_years = (r.groupby("year")["ls"].mean() > 0).sum()
    n_years = r["year"].nunique()

    print(f"concentration 多空(持有{hold_days}天, top20% vs bottom20%):")
    print(f"  毛年化={gross*100:+.2f}%  夏普={sharpe:.2f}  正年份={pos_years}/{n_years}  周期数={len(r)}")
    yearly = r.groupby("year")["ls"].mean() * periods_per_year
    print("  分年度毛年化: " + " ".join(f"{str(y)[-2:]}:{v*100:+.1f}%" for y, v in yearly.items()))


if __name__ == "__main__":
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(hold)
