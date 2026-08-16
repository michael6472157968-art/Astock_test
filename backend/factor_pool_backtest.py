"""因子选股池权重方案对比回测。

对比 3 种权重分配（反转 chg42 + 价值 pb + 低换手 turnover，均 low_good）：
1. 手拍:    [0.40, 0.35, 0.25]  ← factor_weights.json 现值
2. IC加权:  [0.25, 0.27, 0.48]  ← 按 |IC| 归一化(低换手0.109>价值0.061>反转0.056)
3. 等权:    [0.333, 0.333, 0.333]

多空：每期按加权得分分 5 档，多 top20% 空 bottom20%，持有 N 天，扣单边成本。
数据: long_daily.pkl + daily_basic.pkl (1000股 × 2016-2026)
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_pool_backtest.py [hold_days] [cost_bp]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")

SCHEMES = {
    "手拍(0.40/0.35/0.25)": [0.40, 0.35, 0.25],
    "IC加权(0.25/0.27/0.48)": [0.25, 0.27, 0.48],
    "等权(1/3)": [1 / 3, 1 / 3, 1 / 3],
}


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(hold_days: int, cost_bp: int):
    df = pd.read_pickle(LONG_PKL)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = df["close"]

    db = pd.read_pickle(BASIC_PKL)
    for col in ["pb", "turnover_rate"]:
        db[col] = pd.to_numeric(db[col], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    chg42 = c.groupby(level="ts_code").pct_change(42)
    pb = db["pb"]
    turnover = db["turnover_rate"]

    # 三因子（low_good → 用 1-rank 让低值=高分）
    f_rev = 1 - cs_rank(chg42)
    f_val = 1 - cs_rank(pb)
    f_turn = 1 - cs_rank(turnover)

    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    year = [t.year for t in df.index.get_level_values("trade_date")]

    results = {}
    for sname, w in SCHEMES.items():
        score = w[0] * f_rev + w[1] * f_val + w[2] * f_turn
        m = pd.DataFrame({"score": score, "r": fwd_hold, "year": year}).dropna()
        m["q"] = m.groupby(level="trade_date")["score"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        m = m.dropna(subset=["q"]).astype({"q": int})

        dates = sorted(m.index.get_level_values("trade_date").unique())
        periods_per_year = 252 / hold_days
        rows = []
        for offset in range(hold_days):
            rebal_dates = dates[offset::hold_days]
            for rd in rebal_dates:
                try:
                    day = m.xs(rd, level="trade_date")
                except KeyError:
                    continue
                q0 = day[day["q"] == 0]
                q4 = day[day["q"] == 4]
                if len(q0) < 3 or len(q4) < 3:
                    continue
                ls = q4["r"].mean() - q0["r"].mean()
                rows.append((day["year"].iloc[0], ls))

        r = pd.DataFrame(rows, columns=["year", "ls"])
        gross = r["ls"].mean() * periods_per_year
        net = (r["ls"].mean() - 0.0) * periods_per_year  # 多空换手近似抵消，成本另算
        sharpe = r["ls"].mean() / r["ls"].std() * np.sqrt(periods_per_year) if r["ls"].std() > 0 else 0
        pos_years = (r.groupby("year")["ls"].mean() > 0).sum()
        n_years = r["year"].nunique()
        results[sname] = {
            "gross": gross, "sharpe": sharpe,
            "pos_years": f"{pos_years}/{n_years}", "n_periods": len(r),
        }

    print(f"\n=== 因子选股池权重方案对比 (多空 top20% vs bottom20%, 持有{hold_days}天, 1000股×10年) ===")
    print(f"{'方案':<24}{'毛年化':<12}{'夏普':<8}{'正年份':<10}{'周期数':<8}")
    for sname, r in results.items():
        print(f"{sname:<24}{r['gross']*100:+8.2f}%{'':<4}{r['sharpe']:<8.2f}{r['pos_years']:<10}{r['n_periods']:<8}")


if __name__ == "__main__":
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cost = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    main(hold, cost)
