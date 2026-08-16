"""反转因子多空组合回测 — 扣交易成本看净 alpha。

验证 return_21d 反转（横截面 IC -0.07）能否变现：
每天按 return_21d 分 5 组，long Q0(过去跌最多) short Q4(过去涨最多)，
日频调仓，扣单边成本，输出分层收益单调性 + 多空组合年化/夏普/换手/回撤。

用法: cd backend && PYTHONIOENCODING=utf-8 python long_short_backtest.py [sample_size] [cost_bp]
  cost_bp: 单边交易成本（基点），默认 15
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

DB = "data/stock_analyzer.db"


def main(sample_size: int, cost_bp: int):
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
    # 反转因子：过去 21 日累计收益
    factor = c.groupby(level="ts_code").pct_change(21)
    # 未来 1 日收益（正确方向：t+1 相对 t）
    next_ret = c.groupby(level="ts_code").shift(-1) / c - 1

    m = pd.DataFrame({"f": factor, "r": next_ret}).dropna()
    m["q"] = m.groupby(level="trade_date")["f"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    m = m.dropna(subset=["q"])
    m["q"] = m["q"].astype(int)

    # 分层组合日收益
    daily = m.groupby(["trade_date", "q"])["r"].mean().unstack()
    ls_ret = daily[0] - daily[4]  # long Q0(跌最多) - short Q4(涨最多)

    # 换手率（long+short 两端成员变化，索引对齐 ls_ret）
    members = m[m["q"].isin([0, 4])].index.get_level_values("ts_code")
    by_day = m[m["q"].isin([0, 4])].groupby(level="trade_date").apply(
        lambda g: set(g.index.get_level_values("ts_code"))
    )
    dates = sorted(by_day.index)
    to = {}
    for i in range(1, len(dates)):
        prev, cur = by_day.iloc[i - 1], by_day.iloc[i]
        if not prev:
            continue
        to[dates[i]] = len(prev - cur) / len(prev)
    turnover = pd.Series(to)

    cost = cost_bp / 10000.0
    net_ret = ls_ret - turnover * 2 * cost
    net_ret = net_ret.dropna()

    n_days = len(ls_ret)
    def ann(s):
        cum = (1 + s).prod()
        years = n_days / 252
        return cum ** (1 / years) - 1 if years > 0 and cum > 0 else 0.0
    def sharpe(s):
        return s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0.0
    def maxdd(s):
        cum = (1 + s).cumprod()
        return (cum / cum.cummax() - 1).min()

    print(f"\n=== 反转因子(return_21d)多空回测 (样本{len(codes)}股, {n_days}交易日, 单边成本{cost_bp}bp) ===")
    print(f"\n分层组合年化收益（Q0=过去跌最多 → Q4=过去涨最多）:")
    for q in range(5):
        r = daily[q].dropna()
        print(f"  Q{q} 年化 {ann(r)*100:+.2f}%  日胜率 {(r>0).mean()*100:.1f}%")

    print(f"\n多空组合 (long Q0 / short Q4):")
    print(f"  毛收益 年化 {ann(ls_ret)*100:+.2f}%   夏普 {sharpe(ls_ret):.2f}   最大回撤 {maxdd(ls_ret)*100:.1f}%")
    print(f"  净收益 年化 {ann(net_ret)*100:+.2f}%   夏普 {sharpe(net_ret):.2f}   最大回撤 {maxdd(net_ret)*100:.1f}%")
    print(f"  平均单日换手 {turnover.mean()*100:.1f}%（双边）")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    cost = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    main(sample, cost)
