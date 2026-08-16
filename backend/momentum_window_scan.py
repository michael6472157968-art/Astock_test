"""反转窗口细粒度扫描 — 确认 42d 是否峰值，附近有无更优窗口。

rev_42d 已被证实优于 rev_21d（夏普 1.15 vs 1.04，2021 抱团年有效）。
本脚本扫 20-70d 细粒度窗口（步长 5d + 关键点），先 IC 粗筛找峰值，
再自动对 IC 最强前 4 个窗口做多空回测（精筛），锁定最优反转窗口。

用法: cd backend && PYTHONIOENCODING=utf-8 python momentum_window_scan.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")

WINDOWS = [20, 25, 30, 35, 40, 42, 45, 49, 50, 55, 60, 63, 70]


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def backtest(m: pd.DataFrame, name: str, hold_days: int, cost_bp: int) -> float:
    dates = sorted(m.index.get_level_values("trade_date").unique())
    ppy = 252 / hold_days
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
            rows.append((day["year"].iloc[0], ls, to))
            prev = q4
    r = pd.DataFrame(rows, columns=["year", "ls", "to"])
    r["net"] = r["ls"] - r["to"] * 2 * (cost_bp / 10000)
    net = r["net"].mean() * ppy
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
    pos_years = (r.groupby("year")["net"].mean() > 0).sum()
    n_years = r["year"].nunique()
    y2021 = r[r["year"] == "2021"]["net"].mean() * ppy if "2021" in r["year"].values else np.nan
    print(f"\n[rev_{name}d] 净 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}  换手 {r['to'].mean()*100:.1f}%  2021:{y2021*100:+.2f}%")
    return net


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])
    c = df["close"]

    # ── 粗筛：IC 扫描全部窗口（fwd=20）──
    fwd = 20
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)
    print(f"\n=== 反转窗口细粒度 IC 扫描 (fwd={fwd}) ===")
    print(f"{'窗口':<8}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}")
    ic_results = []
    for n in WINDOWS:
        rev = -c.groupby(level="ts_code").pct_change(n)
        f_rank = cs_rank(rev)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        ic_results.append((n, mean_ic, t, icir))
        print(f"{n:<8}{mean_ic:<10.4f}{t:<10.2f}{icir:<8.3f}")

    # 峰值
    ic_results.sort(key=lambda x: -x[1])
    print(f"\nIC 峰值: rev_{ic_results[0][0]}d (IC {ic_results[0][1]:.4f})")
    top_windows = sorted([x[0] for x in ic_results[:4]])

    # ── 精筛：回测 IC 最强前 4 个窗口 + 42d 基准 ──
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    base = pd.DataFrame({"r": fwd_hold, "close": c,
                         "year": [t[:4] for t in df.index.get_level_values("trade_date")]})
    base = base[base["close"] >= 3].dropna(subset=["r"])

    print(f"\n=== 精筛回测 (持有{hold_days}天, 成本{cost_bp}bp, 剔股价<3元) ===")
    for n in top_windows + [42]:
        if n not in top_windows:
            continue
        rev = -c.groupby(level="ts_code").pct_change(n)
        mm = base[["r", "year"]].copy()
        mm["f"] = cs_rank(rev)
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, str(n), hold_days, cost_bp)


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
