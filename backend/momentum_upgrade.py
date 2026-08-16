"""反转窗口升级验证 — rev_42d 替代 rev_21d 是否提升量价合成。

精筛发现 rev_42d（2月反转）全面优于 rev_21d：更高净收益/夏普、更低换手、2021抱团年有效。
本脚本验证：把反转腿从 21d 换成 42d 后，反转+量价背离合成是否提升。

用法: cd backend && PYTHONIOENCODING=utf-8 python momentum_upgrade.py [cost_bp] [hold_days]
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


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).cov(g["b"])
    ).reset_index(level=0, drop=True)


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
    print(f"\n[{name}] 净 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}  换手 {r['to'].mean()*100:.1f}%")
    for year, g in r.groupby("year"):
        print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%")
    return net


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])
    c, h, v = df["close"], df["high"], df["vol"]

    rev21 = -c.groupby(level="ts_code").pct_change(21)
    rev42 = -c.groupby(level="ts_code").pct_change(42)
    a101_16 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 20))

    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    m = pd.DataFrame({"rev21": rev21, "rev42": rev42, "a101_16": a101_16, "r": fwd_hold,
                      "close": c, "year": [t[:4] for t in df.index.get_level_values("trade_date")]})
    m = m[m["close"] >= 3].dropna(subset=["r"])

    print(f"\n=== 反转窗口升级验证 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")

    def run(factor, name):
        mm = m[["r", "year"]].copy()
        mm["f"] = factor
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, name, hold_days, cost_bp)

    run(zscore(cs_rank(m["rev21"])) + zscore(cs_rank(m["a101_16"])), "rev_21d + a101_16 (原配置)")
    run(zscore(cs_rank(m["rev42"])) + zscore(cs_rank(m["a101_16"])), "rev_42d + a101_16 (升级)")
    run(zscore(cs_rank(m["rev21"])) + zscore(cs_rank(m["rev42"])) + zscore(cs_rank(m["a101_16"])), "rev_21d + rev_42d + a101_16 (双反转)")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
