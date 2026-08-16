"""市场状态开关验证 — 指数 MA20 判牛熊，牛市关反转。

对比「开关前 vs 开关后」的反转因子多空收益，重点看：
1. 整体净收益是否提升
2. 2021 牛市 -8.6% 是否被躲开
3. 熊市年收益是否保留

开关：上证指数 close > MA20 = 牛（关反转），否则熊/震荡（开反转）。

用法: cd backend && PYTHONIOENCODING=utf-8 python market_state_switch.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")
IDX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "index_daily.pkl")


def main(cost_bp: int, hold_days: int):
    df = pd.read_pickle(PKL)
    for col in ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    idx = pd.read_pickle(IDX)
    idx = idx[idx["name"] == "上证"].set_index("trade_date")
    bull = idx["bull"]  # trade_date -> bool（close > MA20）

    c = df["close"]
    factor = c.groupby(level="ts_code").pct_change(21)  # return_21d 反转
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    m = pd.DataFrame({"f": factor, "r": fwd_hold,
                      "year": [t[:4] for t in df.index.get_level_values("trade_date")]}).dropna()
    m["q"] = m.groupby(level="trade_date")["f"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"]).astype({"q": int})

    dates = sorted(m.index.get_level_values("trade_date").unique())
    ppy = 252 / hold_days
    cost = cost_bp / 10000

    def run_backtest(use_switch: bool, name: str):
        rows = []
        for offset in range(hold_days):
            prev = None
            for rd in dates[offset::hold_days]:
                # 市场状态（无前视：bull 用 t 日的 MA20，回溯计算）
                is_bull = bool(bull.get(rd, False))
                if use_switch and is_bull:
                    continue  # 牛市关反转，不交易
                try:
                    day = m.xs(rd, level="trade_date")
                except KeyError:
                    continue
                q0 = set(day.index[day["q"] == 0])
                q4 = set(day.index[day["q"] == 4])
                if len(q0) < 3 or len(q4) < 3:
                    continue
                ls = day.loc[day["q"] == 0, "r"].mean() - day.loc[day["q"] == 4, "r"].mean()
                to = len(prev - q0) / len(prev) if prev else 0.0
                rows.append((day["year"].iloc[0], ls, to))
                prev = q0

        r = pd.DataFrame(rows, columns=["year", "ls", "to"])
        r["net"] = r["ls"] - r["to"] * 2 * cost
        gross = r["ls"].mean() * ppy
        net = r["net"].mean() * ppy
        sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
        pos_years = (r.groupby("year")["net"].mean() > 0).sum()
        n_years = r["year"].nunique()
        print(f"\n[{name}] 毛 {gross*100:+.2f}%  净 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}")
        for year, g in r.groupby("year"):
            print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%  (周期{len(g)})")
        return net

    print(f"\n=== 市场状态开关验证 (反转因子, 持有{hold_days}天, 成本{cost_bp}bp) ===")
    run_backtest(False, "开关前（全年开反转）")
    run_backtest(True, "开关后（上证 close>MA20 关反转）")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
