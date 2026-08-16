"""财务因子（现金流/成长/价值）+ 量价因子 多空回测合成 — 正交性最终验证。

IC 检验已收敛：基本面维度最强的是
- 现金流质量：cfps_yoy(t=16) / ocf_yoy(t=15) / ocfps(t=10) / ocf_to_debt(t=10)
- 成长：dt_netprofit_yoy(t=8) / roe_yoy(t=7)
- 价值：BP(t=18) / SP(t=17) / DP(t=15)
- 盈利质量（ROE/ROA/毛利率）证伪无 alpha

本脚本验证：现金流+成长+价值（季度频 PIT）能否变现，与量价腿（反转+量价背离）合成是否提升。

用法: cd backend && PYTHONIOENCODING=utf-8 python fundamental_quality_backtest.py [cost_bp] [hold_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quality_growth_ic import load_pit_financial

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
    print(f"\n[{name}] 毛年化 {gross*100:+.2f}%  净年化 {net*100:+.2f}%  夏普 {sharpe:.2f}  正年份 {pos_years}/{n_years}  换手 {r['to'].mean()*100:.1f}%")
    for year, g in r.groupby("year"):
        print(f"    {year}: 净 {(g['net'].mean()*ppy*100):+6.2f}%")
    return net


def main(cost_bp: int, hold_days: int):
    # ── 量价（long_daily，字符串 trade_date index）──
    base = pd.read_pickle(LONG_PKL)
    for col in ["high", "low", "close", "vol"]:
        base[col] = pd.to_numeric(base[col], errors="coerce")
    base = base.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = base["close"], base["high"], base["vol"]
    ret42 = c.groupby(level="ts_code").pct_change(42)
    a101_16 = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 20))

    # ── 价值（daily_basic，字符串 index）──
    val = pd.read_pickle(DB_PKL)
    for col in ["pb", "ps_ttm", "dv_ttm"]:
        val[col] = pd.to_numeric(val[col], errors="coerce")
    val = val.dropna(subset=["pb"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    bp = 1.0 / val["pb"].replace(0, np.nan)
    sp = 1.0 / val["ps_ttm"].replace(0, np.nan)
    dp = val["dv_ttm"]

    # ── 现金流/成长（PIT 展开，datetime index → 转回字符串）──
    pit = load_pit_financial()
    pit = pit.reset_index()
    pit["trade_date"] = pit["trade_date"].dt.strftime("%Y%m%d")
    pit = pit.set_index(["ts_code", "trade_date"])

    cfps_yoy = pit["cfps_yoy"]
    ocf_yoy = pit["ocf_yoy"]
    ocfps = pit["ocfps"]
    ocf_to_debt = pit["ocf_to_debt"]
    dt_np_yoy = pit["dt_netprofit_yoy"]
    roe_yoy = pit["roe_yoy"]

    # ── 对齐 ──
    factors = pd.DataFrame({
        "ret42_rev": -ret42,
        "a101_16": a101_16,
        "bp": bp, "sp": sp, "dp": dp,
        "cfps_yoy": cfps_yoy, "ocf_yoy": ocf_yoy, "ocfps": ocfps, "ocf_to_debt": ocf_to_debt,
        "dt_np_yoy": dt_np_yoy, "roe_yoy": roe_yoy,
    })
    fwd_hold = c.groupby(level="ts_code").shift(-hold_days) / c - 1
    m = factors.join(fwd_hold.rename("r"), how="inner").dropna()
    m["year"] = [t[:4] for t in m.index.get_level_values("trade_date")]
    # 退市风险过滤：剔除股价 < 3 元（面值退市主力，反转/价值因子的坑）
    m = m.join(c.rename("close"), how="left")
    m = m[m["close"] >= 3].dropna(subset=["r"])

    print(f"\n=== 财务因子 + 量价 多空合成 (1000股 × 10年, 持有{hold_days}天, 成本{cost_bp}bp) ===")

    def run(factor, name):
        mm = m[["r", "year"]].copy()
        mm["f"] = factor
        mm = mm.dropna(subset=["f"])
        mm["q"] = mm.groupby(level="trade_date")["f"].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
        )
        mm = mm.dropna(subset=["q"]).astype({"q": int})
        backtest(mm, name, hold_days, cost_bp)

    # 合成定义
    cashflow = zscore(cs_rank(m["cfps_yoy"])) + zscore(cs_rank(m["ocf_yoy"])) + zscore(cs_rank(m["ocfps"])) + zscore(cs_rank(m["ocf_to_debt"]))
    growth = zscore(cs_rank(m["dt_np_yoy"])) + zscore(cs_rank(m["roe_yoy"]))
    value = zscore(cs_rank(m["bp"])) + zscore(cs_rank(m["sp"])) + zscore(cs_rank(m["dp"]))
    fundamental = cashflow + growth + value
    quant_leg = zscore(cs_rank(m["ret42_rev"])) + zscore(cs_rank(m["a101_16"]))

    # 回测
    run(cashflow, "现金流合成 cfps_yoy+ocf_yoy+ocfps+ocf_to_debt")
    run(growth, "成长合成 dt_np_yoy+roe_yoy")
    run(value, "价值合成 bp+sp+dp")
    run(fundamental, "基本面合成 现金流+成长+价值")
    run(quant_leg, "量价合成 反转+量价背离(原腿)")
    run(fundamental + quant_leg, "全合成 基本面+量价")


if __name__ == "__main__":
    cost = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(cost, hold)
