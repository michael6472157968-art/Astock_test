"""Alpha101 量价协方差因子 a101_16 多空回测 — 10年跨牛熊。

a101_16 = -rank(covariance(rank(high), rank(volume), 5))，IC +0.056（全场最强）。
验证其变现能力，对比反转因子 return_21d（IC -0.057，净 +18%/年）。

用法: cd backend && PYTHONIOENCODING=utf-8 python long_short_a101.py [cost_bp] [hold_days]
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
    for col in ["high", "low", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c, h, v = df["close"], df["high"], df["vol"]

    # a101_16 = -cs_rank(ts_cov(cs_rank(high), cs_rank(vol), 5))
    a101_16 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 5))
    # 对比因子：return_21d 反转
    ret21 = c.groupby(level="ts_code").pct_change(21)

    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    def backtest(factor, name):
        m = pd.DataFrame({"f": factor, "r": fwd_hold, "year": [t[:4] for t in df.index.get_level_values("trade_date")]}).dropna()
        m["q"] = m.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        m = m.dropna(subset=["q"]).astype({"q": int})
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
                # IC 正 → long Q4(因子最高)，IC 负 → long Q0(因子最低)
                # 这里统一输出「正向多空」= long 高因子组 - 低因子组
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
        print(f"\n[{name}] 毛年化 {gross*100:+.2f}%  净年化 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}")
        # 分年度
        for year, g in r.groupby("year"):
            print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%  换手 {g['to'].mean()*100:4.1f}%")
        return net

    print(f"\n=== Alpha101 量价因子 vs 反转 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")
    backtest(a101_16, "a101_16 量价协方差(IC正)")
    backtest(-ret21, "return_21d 反转(取负, IC正)")

    # 合成因子：z-score 等权
    def zscore(s):
        return s.groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))

    combo = zscore(a101_16) + zscore(-ret21)
    backtest(combo, "合成 a101_16 + 反转(zscore等权)")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
