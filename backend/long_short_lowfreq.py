"""低频反转多空回测 — 月频调仓 vs 日频，判断反转能否靠降频变现。

日频反转多空毛收益 +2.65% 年化，被 17%/天换手吃掉。本脚本测低频：
每 hold_days 天调仓一次，持有 hold_days 天，换手大幅下降，看净 alpha 是否转正。

用法: cd backend && PYTHONIOENCODING=utf-8 python long_short_lowfreq.py [sample_size] [cost_bp] [hold_days]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

DB = "data/stock_analyzer.db"


def main(sample_size: int, cost_bp: int, hold_days: int):
    conn = sqlite3.connect(DB)
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

    codes = pd.read_sql(
        "SELECT ts_code FROM stocks WHERE list_date < ? AND name NOT LIKE '%ST%' "
        "ORDER BY RANDOM() LIMIT ?",
        conn, params=[year_ago, sample_size * 2],
    )["ts_code"].tolist()[:sample_size]

    placeholders = ",".join("?" for _ in codes)
    df = pd.read_sql(
        f"SELECT ts_code, trade_date, close FROM stock_daily "
        f"WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date",
        conn, params=codes,
    )
    conn.close()

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c = df["close"]
    factor = c.groupby(level="ts_code").pct_change(21)          # 过去21日收益
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1  # 未来 hold_days 日收益

    m = pd.DataFrame({"f": factor, "r": fwd_hold}).dropna()
    m["q"] = m.groupby(level="trade_date")["f"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"]).astype({"q": int})

    dates = sorted(m.index.get_level_values("trade_date").unique())

    # offset 滚动：多个非重叠调仓序列，增加样本
    all_ls = []   # 每个调仓周期的多空收益（未来 hold_days 日）
    all_to = []   # long 端换手
    q0_returns = []  # 分层收益（Q0..Q4）
    q4_returns = []

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
            r0 = day.loc[day["q"] == 0, "r"].mean()
            r4 = day.loc[day["q"] == 4, "r"].mean()
            all_ls.append(r0 - r4)
            q0_returns.append(r0)
            q4_returns.append(r4)
            if prev_q0:
                all_to.append(len(prev_q0 - q0) / len(prev_q0))
            prev_q0 = q0

    ls = pd.Series(all_ls)
    to = pd.Series(all_to)
    n_periods = len(ls)
    periods_per_year = 252 / hold_days

    gross_ann = ls.mean() * periods_per_year
    net_ann = gross_ann - to.mean() * 2 * (cost_bp / 10000) * periods_per_year
    gross_sharpe = ls.mean() / ls.std() * np.sqrt(periods_per_year) if ls.std() > 0 else 0

    print(f"\n=== 低频反转多空回测 (样本{len(codes)}股, 持有{hold_days}天, {n_periods}个调仓周期, 单边成本{cost_bp}bp) ===")
    print(f"Q0(过去跌最多) 平均周期收益 {pd.Series(q0_returns).mean()*100:+.2f}%")
    print(f"Q4(过去涨最多) 平均周期收益 {pd.Series(q4_returns).mean()*100:+.2f}%")
    print(f"\n多空组合 (long Q0 / short Q4):")
    print(f"  毛收益 年化 {gross_ann*100:+.2f}%   夏普 {gross_sharpe:.2f}")
    print(f"  净收益 年化 {net_ann*100:+.2f}%")
    print(f"  平均调仓换手 {to.mean()*100:.1f}%（long 端）  年化成本 {to.mean()*2*(cost_bp/10000)*periods_per_year*100:.2f}%")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    cost = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    hold = int(sys.argv[3]) if len(sys.argv) > 3 else 21
    main(sample, cost, hold)
