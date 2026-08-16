"""长周期反转因子精筛（分层筛选第 2 步）。

粗筛结论：A 股全程反转（42d/63d/126d/252d 全负 IC），mom_42d 反转最强(-0.073)。
精筛：多空回测验证 -mom_42d/-mom_63d/-mom_126d 变现能力，对比 -return_21d，
看更长窗口反转是否换手更低、收益更高、能否补充/替代 21d 反转。

用法: cd backend && PYTHONIOENCODING=utf-8 python momentum_backtest.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def zscore(s):
    return s.groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))


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
    gross = r["ls"].mean() * ppy
    net = r["net"].mean() * ppy
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
    pos_years = (r.groupby("year")["net"].mean() > 0).sum()
    n_years = r["year"].nunique()
    print(f"\n[{name}] 毛 {gross*100:+.2f}%  净 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}  换手 {r['to'].mean()*100:.1f}%")
    for year, g in r.groupby("year"):
        print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%")
    return net


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])
    c = df["close"]

    F: dict[str, pd.Series] = {}
    for n in [21, 42, 63, 126]:
        F[f"rev_{n}d"] = -c.groupby(level="ts_code").pct_change(n)   # 反转因子

    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    m = pd.DataFrame(F)
    m["r"] = fwd_hold
    m["year"] = [t[:4] for t in m.index.get_level_values("trade_date")]
    # 退市风险过滤
    m["close"] = c
    m = m[m["close"] >= 3].dropna(subset=["r"])

    print(f"\n=== 长周期反转 vs 21d 反转 多空回测 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")

    def run(factor, name):
        mm = m[["r", "year"]].copy()
        mm["f"] = cs_rank(factor)
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, name, hold_days, cost_bp)

    for n in [21, 42, 63, 126]:
        run(F[f"rev_{n}d"], f"rev_{n}d 反转")

    # 合成：21d + 42d（看长窗口是否补充短窗口）
    combo = zscore(cs_rank(F["rev_21d"])) + zscore(cs_rank(F["rev_42d"]))
    run(combo, "rev_21d + rev_42d 合成")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
