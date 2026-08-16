"""多因子加权优化 — 滚动 IC 加权 vs 等权（无前视）。

因子：a101_16（量价背离）+ -return_21d（反转），当前等权合成。
本脚本测「滚动 IC 加权」是否更好：
- 每个时点 t，用「过去 N 日已实现的横截面 IC」作为因子权重（严格无前视：IC 的标签都发生在 t 之前）
- 对比等权 vs 滚动 IC 加权（252日窗口 vs 60日窗口）

用法: cd backend && PYTHONIOENCODING=utf-8 python factor_weighting.py [cost_bp] [hold_days]
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


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c, h, v = df["close"], df["high"], df["vol"]

    # 两个因子（方向都调成「高值→未来涨」）
    f1 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 5))  # a101_16 量价背离
    f2 = -c.groupby(level="ts_code").pct_change(21)   # -return_21d 反转

    fwd = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    # 逐日横截面 IC（每天 corr(rank(factor), rank(future_ret))）
    r_rank = cs_rank(fwd)
    def daily_ic(f):
        tmp = pd.DataFrame({"f": cs_rank(f), "r": r_rank}).dropna()
        return tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))

    ic1 = daily_ic(f1).rename("ic1")
    ic2 = daily_ic(f2).rename("ic2")

    # 滚动 IC 权重（shift hold_days 确保无前视：只用已实现的 IC）
    def rolling_weight(ic, window):
        return ic.rolling(window).mean().shift(hold_days)

    # 横截面 zscore
    def zscore(s):
        return s.groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))

    z1, z2 = zscore(f1), zscore(f2)

    combos = {
        "等权": (z1 + z2) / 2,
        "滚动IC加权(252日)": _weighted_combo(z1, z2, ic1, ic2, 252, hold_days),
        "滚动IC加权(60日)": _weighted_combo(z1, z2, ic1, ic2, 60, hold_days),
    }

    print(f"\n=== 多因子加权优化 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")
    for name, factor in combos.items():
        net, sharpe, pos, n_years, years = _backtest(factor, fwd, df, hold_days, cost_bp)
        print(f"\n[{name}] 净 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos}/{n_years}")
        for year, g in years.groupby("year"):
            print(f"    {year}: 净 {(g['net'].mean()*252/hold_days*100):+6.2f}%")


def _weighted_combo(z1, z2, ic1, ic2, window, hold_days):
    w1 = ic1.rolling(window).mean().shift(hold_days)
    w2 = ic2.rolling(window).mean().shift(hold_days)
    total = w1.abs() + w2.abs()
    # 权重 = IC 占两个因子 IC 绝对值之和的比例
    a = (w1.abs() / total.replace(0, np.nan)).fillna(0.5)
    b = 1 - a
    # 对齐 index（multi-index）
    a_aligned = a.reindex(z1.index, level="trade_date").fillna(0.5)
    b_aligned = b.reindex(z1.index, level="trade_date").fillna(0.5)
    # 权重还要按 IC 符号调方向（两个因子 IC 应该都为正，但保守起见乘符号）
    s1 = np.sign(w1.reindex(z1.index, level="trade_date")).fillna(1)
    s2 = np.sign(w2.reindex(z1.index, level="trade_date")).fillna(1)
    return a_aligned * s1 * z1 + b_aligned * s2 * z2


def _backtest(factor, fwd, df, hold_days, cost_bp):
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
            rows.append((day["year"].iloc[0], ls, to))
            prev = q4

    r = pd.DataFrame(rows, columns=["year", "ls", "to"])
    r["net"] = r["ls"] - r["to"] * 2 * cost
    net = r["net"].mean() * ppy
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
    pos = (r.groupby("year")["net"].mean() > 0).sum()
    n_years = r["year"].nunique()
    return net, sharpe, pos, n_years, r


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
