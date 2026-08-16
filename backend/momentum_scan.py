"""长周期动量因子粗筛（分层筛选第 1 步）。

补齐数据周期维度的空白：超短/短/中已挖（反转 21d、低波动、价值），
长周期（126-252d）动量未系统测。A 股经典疑团：短期反转，中期/长期是动量还是反转？

粗筛：全样本 1000 股 × 10 年，快速 IC 扫描（不做回测），fwd=20/60 双周期。
候选 |t|>3 才进精筛（多空回测）。

因子清单：
- 直接动量 mom_42/63/126/252d
- 跳过近期(21d)的经典动量 mom_3_1/6_1/12_1（Jegadeesh-Titman 12-1）
- 长期反转 rev_126/252d（若动量为负 IC 则长期也是反转）

用法: cd backend && PYTHONIOENCODING=utf-8 python momentum_scan.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main():
    df = pd.read_pickle(PKL)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])
    c = df["close"]

    # 长周期动量/反转因子
    F: dict[str, pd.Series] = {}
    for n in [42, 63, 126, 252]:
        F[f"mom_{n}d"] = c.groupby(level="ts_code").pct_change(n)          # 直接动量
    F["mom_3_1"] = c.groupby(level="ts_code").shift(21) / c.groupby(level="ts_code").shift(63) - 1   # 3月跳过1月
    F["mom_6_1"] = c.groupby(level="ts_code").shift(21) / c.groupby(level="ts_code").shift(126) - 1  # 6月跳过1月
    F["mom_12_1"] = c.groupby(level="ts_code").shift(21) / c.groupby(level="ts_code").shift(252) - 1 # 12月跳过1月(JT经典)
    F["rev_126d"] = -c.groupby(level="ts_code").pct_change(126)
    F["rev_252d"] = -c.groupby(level="ts_code").pct_change(252)

    for fwd in [20, 60]:
        fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
        r_rank = cs_rank(fwd_ret)
        print(f"\n=== 长周期动量/反转 粗筛 IC (1000股 × 10年, 前向{fwd}日) ===")
        print(f"{'因子':<14}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}{'方向解读':<16}")
        results = []
        for name, f in F.items():
            f_rank = cs_rank(f)
            tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
            ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
            ic = ic.dropna()
            if len(ic) < 30:
                continue
            mean_ic = ic.mean()
            std_ic = ic.std()
            t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
            icir = mean_ic / std_ic if std_ic > 0 else 0.0
            interp = "动量(追涨)" if mean_ic > 0 else "反转(买跌)"
            results.append((name, mean_ic, t, icir, interp))
        results.sort(key=lambda x: -abs(x[2]))
        for name, mic, t, icir, interp in results:
            mark = " ★" if abs(t) > 3 else ""
            print(f"{name:<14}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{interp:<16}{mark}")


if __name__ == "__main__":
    main()
