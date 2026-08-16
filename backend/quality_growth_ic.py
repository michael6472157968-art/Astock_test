"""质量 + 成长因子横截面 IC 检验（季度频 fina_indicator，point-in-time 对齐）。

fina_indicator 是季度频财务指标。做横截面 IC 必须避免未来函数：
- 每条记录有 end_date（报告期）+ ann_date（实际公告日）
- 用 ann_date 做 point-in-time：交易日 t 只能用到「ann_date <= t」的最近一期财务值
- merge_asof backward 实现（同 ts_code 内，每个交易日匹配最近已公告的财务期）

反推 Tushare 财务因子库：
- Quality（质量，59 个核心）: roe/roa/毛利率/净利率/负债率/流动比率/现金流质量/周转率
- Growth（成长，15 个核心）: 营收/净利/EPS/营业利润/ROE 同比增速

数据：data/fina_indicator.pkl + data/long_daily.pkl（价格做 forward return）。
用法: cd backend && PYTHONIOENCODING=utf-8 python quality_growth_ic.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FIN_PKL = os.path.join(DATA_DIR, "fina_indicator.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")


# ── 质量因子（高=好，正 IC 预期）──
QUALITY_FACTORS = {
    "roe": "roe",
    "roe_waa": "roe_waa",             # 加权ROE
    "roe_dt": "roe_dt",               # 扣非ROE
    "roe_yearly": "roe_yearly",       # 年化ROE
    "roa": "roa",
    "roa2_yearly": "roa2_yearly",     # 年化ROA
    "roic": "roic",                   # 投入资本回报率
    "npta": "npta",                   # 总资产净利率
    "grossprofit_margin": "grossprofit_margin",  # 毛利率
    "netprofit_margin": "netprofit_margin",      # 净利率
    "profit_to_gr": "profit_to_gr",   # 利润/营收
    "op_of_gr": "op_of_gr",           # 营业利润/营收
    "assets_turn": "assets_turn",     # 总资产周转率
    "current_ratio": "current_ratio", # 流动比率
    "quick_ratio": "quick_ratio",     # 速动比率
    "cash_ratio": "cash_ratio",       # 现金比率
    "ocfps": "ocfps",                 # 每股经营现金流
    "cfps": "cfps",                   # 每股现金流
    "q_ocf_to_sales": "q_ocf_to_sales",  # 单季经营现金流/营收
    "ocf_to_debt": "ocf_to_debt",     # 经营现金流/负债
}

# ── 质量因子（低=好，负 IC 预期）──
QUALITY_INVERSE = {
    "debt_to_assets": "debt_to_assets",  # 资产负债率
    "debt_to_eqt": "debt_to_eqt",        # 产权比率
    "assets_to_eqt": "assets_to_eqt",    # 权益乘数
}

# ── 成长因子（高=好，正 IC 预期）──
GROWTH_FACTORS = {
    "or_yoy": "or_yoy",                 # 营收同比
    "tr_yoy": "tr_yoy",                 # 营收同比(另一口径)
    "netprofit_yoy": "netprofit_yoy",   # 净利同比
    "dt_netprofit_yoy": "dt_netprofit_yoy",  # 扣非净利同比
    "basic_eps_yoy": "basic_eps_yoy",   # EPS同比
    "dt_eps_yoy": "dt_eps_yoy",         # 扣非EPS同比
    "op_yoy": "op_yoy",                 # 营业利润同比
    "ebt_yoy": "ebt_yoy",               # 利润总额同比
    "roe_yoy": "roe_yoy",               # ROE同比
    "ocf_yoy": "ocf_yoy",               # 经营现金流同比
    "cfps_yoy": "cfps_yoy",             # 每股现金流同比
    "bps_yoy": "bps_yoy",               # 每股净资产同比
    "assets_yoy": "assets_yoy",         # 总资产同比
    "eqt_yoy": "eqt_yoy",               # 股东权益同比
    "q_sales_yoy": "q_sales_yoy",       # 单季营收同比
    "q_op_qoq": "q_op_qoq",             # 单季营业利润环比
}


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def load_pit_financial() -> pd.DataFrame:
    """财务指标 point-in-time 展开到日频，返回 multi-index (ts_code, trade_date)。"""
    fin = pd.read_pickle(FIN_PKL)
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    # StringDtype 会破坏 merge_asof 排序判断，转普通 object
    fin["ts_code"] = fin["ts_code"].astype("object")
    price["ts_code"] = price["ts_code"].astype("object")

    # 财务数据清洗
    for col in ["ann_date", "end_date"]:
        fin[col] = pd.to_datetime(fin[col].astype(str), errors="coerce")
    fin = fin.dropna(subset=["ann_date"])
    # 同 (ts_code, end_date) 去重，取最后一条（最新修正）
    fin = fin.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(["ts_code", "end_date"], keep="last")
    # 同 ann_date 可能多期同时公告，取最新 end_date
    fin = fin.sort_values(["ts_code", "ann_date", "end_date"]).drop_duplicates(["ts_code", "ann_date"], keep="last")

    # 交易日 index（价格）
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price = price.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])
    price_idx = price[["ts_code", "trade_date"]]

    # 需要展开的因子列
    factor_cols = list(QUALITY_FACTORS.values()) + list(QUALITY_INVERSE.values()) + list(GROWTH_FACTORS.values())
    keep_cols = ["ts_code", "ann_date", "end_date"] + factor_cols
    fin = fin[keep_cols].copy()
    for c in factor_cols:
        fin[c] = pd.to_numeric(fin[c], errors="coerce")

    # PIT 对齐：同 ts_code 内，交易日匹配最近已公告财务期（ann_date <= trade_date）。
    # 循环 + 无 by 的 merge_asof（by + 扩展dtype 组合有兼容性坑，逐股最稳）。
    fin = fin.sort_values(["ts_code", "ann_date"])
    price_idx = price_idx.sort_values(["ts_code", "trade_date"])
    right_cols = ["ann_date", "end_date"] + factor_cols
    frames = []
    for code, pg in price_idx.groupby("ts_code", sort=False):
        fg = fin[fin["ts_code"] == code]
        if fg.empty:
            continue
        m = pd.merge_asof(
            pg[["trade_date"]].sort_values("trade_date"),
            fg[right_cols].sort_values("ann_date"),
            left_on="trade_date",
            right_on="ann_date",
            direction="backward",
        )
        m["ts_code"] = code
        frames.append(m)
    merged = pd.concat(frames, ignore_index=True)

    merged = merged.set_index(["ts_code", "trade_date"])
    return merged


def main(fwd: int):
    pit = load_pit_financial()
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    price["ts_code"] = price["ts_code"].astype("object")
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price = price.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = price["close"]

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== 质量+成长因子横截面 IC (point-in-time, 前向{fwd}日) ===")

    factors: dict[str, tuple[str, str]] = {}
    for name, col in QUALITY_FACTORS.items():
        factors[f"Q_{name}"] = (col, "high_good")
    for name, col in QUALITY_INVERSE.items():
        factors[f"Q_{name}"] = (col, "low_good")
    for name, col in GROWTH_FACTORS.items():
        factors[f"G_{name}"] = (col, "high_good")

    results = []
    for fname, (col, direction) in factors.items():
        f = pit[col]
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        if len(tmp) < 5000:
            print(f"  {fname}: 有效样本不足 ({len(tmp)}), 跳过")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        # low_good 因子期望负 IC，但这里报告原始方向 IC（读者自己判方向）
        results.append((fname, direction, mean_ic, t, icir, len(ic)))

    results.sort(key=lambda x: -abs(x[3]))
    print(f"{'因子':<26}{'方向':<10}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}{'IC天数':<8}")
    for fname, direction, mic, t, icir, n in results:
        mark = " ★" if abs(t) > 5 else ""
        expect = "(-)" if direction == "low_good" else "(+)"
        print(f"{fname:<26}{expect:<10}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{n:<8}{mark}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
