"""10 年跨牛熊反转因子回测 — 分年度看 alpha 稳定性。

用 long_daily.pkl（1000股 × 2016-2026 十年日线）重跑 return_21d 反转多空，
周频调仓（持有5天），输出整体 + 分年度净收益，判断 alpha 是否跨牛熊稳健。

用法: cd backend && PYTHONIOENCODING=utf-8 python long_short_10y.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c = df["close"]
    factor = c.groupby(level="ts_code").pct_change(21)
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    m = pd.DataFrame({"f": factor, "r": fwd_hold, "year": [t[:4] for t in df.index.get_level_values("trade_date")]}).dropna()
    m["q"] = m.groupby(level="trade_date")["f"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"]).astype({"q": int})

    dates = sorted(m.index.get_level_values("trade_date").unique())
    periods_per_year = 252 / hold_days

    # 整体：offset 滚动，收集多空收益 + 换手 + 年份
    rows = []  # (year, ls_ret, turnover)
    for offset in range(hold_days):
        rebal_dates = dates[offset::hold_days]
        prev_q0 = None
        for rd in rebal_dates:
            try:
                day = m.xs(rd, level="trade_date")
            except KeyError:
                continue
            q0 = set(day.index[day["q"] == 0])
            q4 = set(day.index[day["q"] == 4])
            if len(q0) < 3 or len(q4) < 3:
                continue
            ls = day.loc[day["q"] == 0, "r"].mean() - day.loc[day["q"] == 4, "r"].mean()
            year = day["year"].iloc[0]
            to = 0.0
            if prev_q0:
                to = len(prev_q0 - q0) / len(prev_q0)
            rows.append((year, ls, to))
            prev_q0 = q0

    r = pd.DataFrame(rows, columns=["year", "ls", "to"])
    r["net"] = r["ls"] - r["to"] * 2 * (cost_bp / 10000)

    print(f"\n=== 10年反转因子多空回测 (1000股, 持有{hold_days}天, 单边成本{cost_bp}bp) ===")
    print(f"整体 {len(r)} 个调仓周期，2016-2026\n")

    # 分年度
    print(f"{'年份':<8}{'周期数':<8}{'毛年化':<12}{'净年化':<12}{'换手':<8}")
    for year, g in r.groupby("year"):
        gross = g["ls"].mean() * periods_per_year
        net = g["net"].mean() * periods_per_year
        to = g["to"].mean()
        print(f"{year:<8}{len(g):<8}{gross*100:+8.2f}%{'':<4}{net*100:+8.2f}%{'':<4}{to*100:5.1f}%")

    # 整体
    gross_all = r["ls"].mean() * periods_per_year
    net_all = r["net"].mean() * periods_per_year
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(periods_per_year) if r["net"].std() > 0 else 0
    pos_years = (r.groupby("year")["net"].mean() > 0).sum()
    n_years = r["year"].nunique()

    print(f"\n整体: 毛年化 {gross_all*100:+.2f}%   净年化 {net_all*100:+.2f}%   夏普 {sharpe:.2f}")
    print(f"分年度净收益为正的年份: {pos_years}/{n_years}")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
