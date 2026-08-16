"""筹码分布因子横截面 IC 检验。

因子（方向预期）：
- winner_rate(获利盘%)：高 = 大部分持仓者获利 = 抛压大 → 看跌（负 IC）
- concentration(筹码宽度) = (cost_95pct-cost_5pct)/weight_avg：窄 = 筹码集中 = 主力控盘 → 看涨（负 IC）
- cost_dev(股价/均成本-1)：高 = 股价远超平均成本 = 获利盘多 → 看跌（负 IC）

数据：data/cyq_perf.pkl + data/long_daily.pkl（close 算 forward return）
用法: cd backend && PYTHONIOENCODING=utf-8 python cyqperf_ic.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    cq = pd.read_pickle(CYQ_PKL)
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]

    for col in ["winner_rate", "cost_5pct", "cost_95pct", "weight_avg"]:
        cq[col] = pd.to_numeric(cq[col], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price["close"] = pd.to_numeric(price["close"], errors="coerce")

    m = cq.merge(price, on=["ts_code", "trade_date"], how="inner")
    m = m.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])

    c = m["close"]
    F = {
        "winner_rate(获利盘%)": m["winner_rate"],
        "concentration(筹码宽度)": (m["cost_95pct"] - m["cost_5pct"]) / m["weight_avg"].replace(0, np.nan),
        "cost_dev(股价/均成本-1)": c / m["weight_avg"].replace(0, np.nan) - 1,
    }

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== 筹码分布因子横截面 IC (1000股, 前向{fwd}日) ===")
    results = []
    for name, f in F.items():
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        if len(tmp) < 5000:
            print(f"  {name}: 有效样本不足 ({len(tmp)})")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        results.append((name, mean_ic, t, icir, len(ic)))
        print(f"  {name:<28} mean IC={mean_ic:+.4f}  t={t:+.2f}  ICIR={icir:+.3f}  IC天数={len(ic)}")

    # 分年度（winner_rate 和 concentration）
    for name, f in [("winner_rate", F["winner_rate(获利盘%)"]), ("concentration", F["concentration(筹码宽度)"])]:
        tmp = pd.DataFrame({"f": cs_rank(f), "r": r_rank}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"])).dropna()
        yearly = ic.groupby(lambda d: str(d)[:4]).mean()
        print(f"\n{name} 分年度 mean IC:")
        print("  " + " ".join(f"{y[-2:]}:{v:+.3f}" for y, v in yearly.items()))


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(fwd)
