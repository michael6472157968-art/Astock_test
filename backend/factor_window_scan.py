"""换手降频 — a101_16 的 cov 窗口扫描（5/10/20日）。

a101_16 = -cs_rank(ts_cov(cs_rank(high), cs_rank(vol), window))，cov 窗口 5 日时换手 74%。
放宽窗口 → 信号变钝 → 换手下降 → 成本降低，但 alpha 可能衰减。
扫描窗口，找「换手降 + alpha 不衰减」的最优点。

用法: cd backend && PYTHONIOENCODING=utf-8 python factor_window_scan.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).cov(g["b"])
    ).reset_index(level=0, drop=True)


def backtest(factor, fwd, df, hold_days, cost_bp):
    m = pd.DataFrame({"f": factor, "r": fwd,
                      "year": [t[:4] for t in df.index.get_level_values("trade_date")]}).dropna()
    m["q"] = m.groupby(level="trade_date")["f"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"]).astype({"q": int})
    dates = sorted(m.index.get_level_values("trade_date").unique())
    ppy = 252 / hold_days
    cost = cost_bp / 10000

    rows = []
    for offset in range(hold_days):
        prev = None
        for rd in dates[offset::hold_days]:
            try:
                day = m.xs(rd, level="trade_date")
            except KeyError:
                continue
            q0 = set(day.index[day["q"] == 0])
            q4 = set(day.index[day["q"] == 4])
            if len(q0) < 3 or len(q4) < 3:
                continue
            ls = day.loc[day["q"] == 4, "r"].mean() - day.loc[day["q"] == 0, "r"].mean()
            to = len(prev - q4) / len(prev) if prev else 0.0
            rows.append((ls, to))
            prev = q4

    r = pd.DataFrame(rows, columns=["ls", "to"])
    r["net"] = r["ls"] - r["to"] * 2 * cost
    net = r["net"].mean() * ppy
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
    return net, sharpe, r["to"].mean()


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c, h, v = df["close"], df["high"], df["vol"]
    fwd = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    print(f"\n=== 换手降频 — a101_16 cov 窗口扫描 (持有{hold_days}天, 成本{cost_bp}bp) ===")
    print(f"{'cov窗口':<10}{'换手':<10}{'毛年化':<12}{'净年化':<12}{'夏普':<8}")

    for window in [3, 5, 10, 20, 40]:
        a101 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), window))
        net, sharpe, to = backtest(a101, fwd, df, hold_days, cost_bp)
        gross = net + to * 2 * (cost_bp / 10000) * (252 / hold_days)
        print(f"{window:<10}{to*100:<10.1f}{gross*100:<12.2f}{net*100:<12.2f}{sharpe:<8.2f}")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
