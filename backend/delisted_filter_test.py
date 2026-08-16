"""退市风险过滤 — 低价股过滤能否恢复反转因子强度。

含退市股回测显示幸存者偏差砍掉 -9.27pp（+20.5%→+11.2%）。本脚本验证：
买入时剔除「股价 < 阈值」的低价股（面值退市主力的明确信号），
看反转+量价背离因子能否在「安全池」里恢复强度。

用法: cd backend && PYTHONIOENCODING=utf-8 python delisted_filter_test.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")
DELIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "delisted_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).cov(g["b"])
    ).reset_index(level=0, drop=True)


def zscore(s):
    return s.groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))


def backtest(df, hold_days, cost_bp, min_price):
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = df["close"], df["high"], df["vol"]

    ret21 = -c.groupby(level="ts_code").pct_change(21)
    a101 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 20))
    combo = zscore(a101) + zscore(ret21)
    fwd = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    m = pd.DataFrame({"f": combo, "r": fwd, "close": c}).dropna()
    # 低价股过滤：买入时剔除 close < min_price
    if min_price > 0:
        m = m[m["close"] >= min_price]

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
            rows.append((ls, to))
            prev = q4

    r = pd.DataFrame(rows, columns=["ls", "to"])
    r["net"] = r["ls"] - r["to"] * 2 * cost
    net = r["net"].mean() * ppy
    sharpe = r["net"].mean() / r["net"].std() * np.sqrt(ppy) if r["net"].std() > 0 else 0
    return net, sharpe, r["to"].mean()


def main(cost_bp: int, hold_days: int):
    base = pd.read_pickle(PKL)
    delisted = pd.read_pickle(DELIST)
    combined = pd.concat([base, delisted], ignore_index=True)

    print(f"\n=== 退市风险过滤 (含退市股{combined['ts_code'].nunique()}只, 成本{cost_bp}bp, 持有{hold_days}天) ===")
    print(f"{'过滤':<20}{'净年化':<12}{'夏普':<8}{'换手':<8}")

    # 不过滤基线
    net, sh, to = backtest(combined, hold_days, cost_bp, 0)
    print(f"{'不过滤':<20}{net*100:<12.2f}{sh:<8.2f}{to*100:<8.1f}")

    for min_price in [1.0, 1.5, 2.0, 3.0, 5.0]:
        net, sh, to = backtest(combined, hold_days, cost_bp, min_price)
        print(f"{'剔除股价<' + str(min_price) + '元':<20}{net*100:<12.2f}{sh:<8.2f}{to*100:<8.1f}")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
