"""价值因子横截面 IC 检验（日频 daily_basic）。

反推 Tushare 财务因子库 Value 类（11 个核心）：
- EP = 1/pe_ttm 盈利收益率（高=低估）
- BP = 1/pb 账面市值比（高=低估）
- SP = 1/ps_ttm 销售收益率（高=低估）
- DP = dv_ttm 股息率（高=高股息）
- LnMC = -log(total_mv) 规模（小市值）
- LnFloatMC = -log(circ_mv) 流通市值

数据：data/daily_basic.pkl（1000股 × 2016-2026 日频）。
横截面 IC：因子日频 rank 与前向收益 rank 的 Spearman 相关。
用法: cd backend && PYTHONIOENCODING=utf-8 python value_factor_ic.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily_basic.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    df = pd.read_pickle(PKL)
    for col in ["close", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm", "total_mv", "circ_mv", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    c = df["close"]
    # 价值因子（1/x 保留符号：负 PE=亏损股自然排最低）
    F: dict[str, pd.Series] = {}
    F["EP_1/pe_ttm"] = 1.0 / df["pe_ttm"].replace(0, np.nan)
    F["BP_1/pb"] = 1.0 / df["pb"].replace(0, np.nan)
    F["SP_1/ps_ttm"] = 1.0 / df["ps_ttm"].replace(0, np.nan)
    F["DP_dv_ttm"] = df["dv_ttm"]
    F["LnMC_-log(total_mv)"] = -np.log(df["total_mv"].replace(0, np.nan))
    F["LnFloatMC_-log(circ_mv)"] = -np.log(df["circ_mv"].replace(0, np.nan))
    # 对照：直接 pe_ttm（负向=低PE涨）、换手率
    F["pe_ttm"] = df["pe_ttm"]
    F["turnover_rate"] = df["turnover_rate"]

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== 价值因子横截面 IC (1000股 × 10年, 前向{fwd}日) ===")
    print(f"样本覆盖: {df['close'].groupby(level='trade_date').count().describe()[['mean','min','max']].round(0).to_dict()}")

    results = []
    for name, f in F.items():
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        if len(tmp) < 1000:
            print(f"  {name}: 有效样本不足 ({len(tmp)}), 跳过")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        # 分年度稳定性（粗略：按年取 mean IC）
        yearly = ic.groupby(lambda d: d[:4]).mean()
        results.append((name, mean_ic, t, icir, len(ic), yearly))

    results.sort(key=lambda x: -abs(x[2]))
    print(f"{'因子':<24}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}{'IC天数':<8}")
    for name, mic, t, icir, n, yearly in results:
        mark = " ★" if abs(t) > 5 else ""
        print(f"{name:<24}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{n:<8}{mark}")

    # 打印年度 IC 明细（top 因子）
    print("\n=== 年度 mean IC 明细（按 |t| 前5） ===")
    for name, mic, t, icir, n, yearly in results[:5]:
        ystr = " ".join(f"{y[-2:]}:{v:+.3f}" for y, v in yearly.items())
        print(f"{name:<24}{ystr}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
