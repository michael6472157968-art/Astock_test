"""含退市股回测 — 消除幸存者偏差。

之前 1000 股采样自「存续股」，退市股被排除 → 反转因子（买跌）收益被高估。
本脚本拉 2016 年后退市的 250 只股票历史日线，加入样本，重跑反转+合成因子，
对比「含退市股 vs 不含」的净收益，量化幸存者偏差。

用法: cd backend && PYTHONIOENCODING=utf-8 python delisted_backtest.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.tushare_client import get_pro

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


def load_delisted():
    if os.path.exists(DELIST):
        return pd.read_pickle(DELIST)
    pro = get_pro()
    delisted = pro.stock_basic(list_status="D", fields="ts_code,name,list_date,delist_date")
    delisted = delisted[delisted["delist_date"].astype(str).str[:4] >= "2016"]
    codes = delisted["ts_code"].tolist()
    print(f"拉取 {len(codes)} 只退市股历史日线...")
    frames = []
    for i, code in enumerate(codes):
        try:
            d = pro.daily(ts_code=code, start_date="20160101", end_date="20260814")
            if d is not None and not d.empty:
                frames.append(d[["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]])
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(codes)}")
    out = pd.concat(frames, ignore_index=True)
    out.to_pickle(DELIST)
    print(f"完成: {len(out)} 行, {out['ts_code'].nunique()} 只退市股 → {DELIST}")
    return out


def backtest(df, hold_days, cost_bp):
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = df["close"], df["high"], df["vol"]

    ret21 = -c.groupby(level="ts_code").pct_change(21)
    a101 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 20))
    combo = zscore(a101) + zscore(ret21)
    fwd = c.groupby(level="ts_code").shift(-hold_days) / c - 1

    m = pd.DataFrame({"f": combo, "r": fwd}).dropna()
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
    print(f"\n=== 含退市股回测 (成本{cost_bp}bp, 持有{hold_days}天) ===")
    net0, sh0, to0 = backtest(base, hold_days, cost_bp)
    print(f"[不含退市股] {base['ts_code'].nunique()}股  净 {net0*100:+.2f}%  夏普 {sh0:.2f}  换手 {to0*100:.1f}%")

    delisted = load_delisted()
    combined = pd.concat([base, delisted], ignore_index=True)
    print(f"\n合并后样本 {combined['ts_code'].nunique()} 股（含 {delisted['ts_code'].nunique()} 退市股）")
    net1, sh1, to1 = backtest(combined, hold_days, cost_bp)
    print(f"[含退市股] {combined['ts_code'].nunique()}股  净 {net1*100:+.2f}%  夏普 {sh1:.2f}  换手 {to1*100:.1f}%")

    print(f"\n幸存者偏差影响: 净收益 {net0*100:+.2f}% → {net1*100:+.2f}%（变化 {(net1-net0)*100:+.2f}pp）")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
