"""财务因子多空回测 + 与量价因子合成（正交性验证）。

价值因子（BP/SP/DP/EP，来自 daily_basic）做多空回测，验证变现能力；
再与已有的两条量价腿（反转 return_21d、量价背离 a101_16）合成，验证正交性。

若合成后夏普/正年份提升 → 基本面是独立第三条腿，可纳入多因子体系。

数据：long_daily.pkl（价格/量）+ daily_basic.pkl（估值）。
用法: cd backend && PYTHONIOENCODING=utf-8 python fundamental_long_short.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
DB_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")


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
    print(f"\n[{name}] 毛年化 {gross*100:+.2f}%  净年化 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}")
    for year, g in r.groupby("year"):
        print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%  换手 {g['to'].mean()*100:4.1f}%")
    return net


def main(cost_bp: int, hold_days: int):
    # ── 价格（long_daily）──
    base = pd.read_pickle(LONG_PKL)
    for col in ["high", "low", "close", "vol"]:
        base[col] = pd.to_numeric(base[col], errors="coerce")
    base = base.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = base["close"], base["high"], base["vol"]

    ret21 = c.groupby(level="ts_code").pct_change(21)
    a101_16 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 20))  # cov=20 换手降频版

    # ── 估值因子（daily_basic）──
    val = pd.read_pickle(DB_PKL)
    for col in ["pe_ttm", "pb", "ps_ttm", "dv_ttm", "total_mv"]:
        val[col] = pd.to_numeric(val[col], errors="coerce")
    val = val.dropna(subset=["pb"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    bp = 1.0 / val["pb"].replace(0, np.nan)
    sp = 1.0 / val["ps_ttm"].replace(0, np.nan)
    dp = val["dv_ttm"]
    ep = 1.0 / val["pe_ttm"].replace(0, np.nan)

    # ── 对齐到价格 index ──
    factors = pd.DataFrame({
        "ret21_rev": -ret21,
        "a101_16": a101_16,
        "bp": bp,
        "sp": sp,
        "dp": dp,
        "ep": ep,
    })
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    m = factors.join(fwd_hold.rename("r"), how="inner").dropna()
    m["year"] = [t[:4] for t in m.index.get_level_values("trade_date")]

    print(f"\n=== 财务因子 vs 量价因子 多空回测 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")

    # 每个因子独立回测
    for col, name in [("bp", "BP 账面市值比(价值)"), ("sp", "SP 销售收益率(价值)"),
                      ("dp", "DP 股息率(价值)"), ("ep", "EP 盈利收益率(价值)"),
                      ("ret21_rev", "return_21d 反转"), ("a101_16", "a101_16 量价背离")]:
        mm = m[["r", "year"]].copy()
        mm["f"] = cs_rank(m[col])
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, name, hold_days, cost_bp)

    # ── 合成：价值等权 / 价值+反转 / 价值+反转+量价背离 ──
    value_combo = zscore(cs_rank(m["bp"])) + zscore(cs_rank(m["sp"])) + zscore(cs_rank(m["dp"]))
    two_combo = value_combo + zscore(cs_rank(m["ret21_rev"]))
    three_combo = two_combo + zscore(cs_rank(m["a101_16"]))

    for combo, name in [(value_combo, "价值合成 BP+SP+DP"),
                        (two_combo, "价值+反转 合成"),
                        (three_combo, "价值+反转+量价背离 合成")]:
        mm = m[["r", "year"]].copy()
        mm["f"] = combo
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, name, hold_days, cost_bp)


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
