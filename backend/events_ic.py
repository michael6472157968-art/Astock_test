"""解禁 + 增减持 事件因子横截面 IC 检验。

因子：
- 增减持净额 = 过去60日净增减持比例（IN增持=+change_ratio, DE减持=-change_ratio）。净增持 → 看涨（正IC预期）
- 解禁压力 = 未来30日累计解禁比例（float_ratio 之和）。高解禁 → 供给冲击看跌（负IC预期）

数据：data/stk_holdertrade.pkl + data/share_float.pkl + data/long_daily.pkl
用法: cd backend && PYTHONIOENCODING=utf-8 python events_ic.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
HT_PKL = os.path.join(DATA_DIR, "stk_holdertrade.pkl")
SF_PKL = os.path.join(DATA_DIR, "share_float.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def _daily_series(events: pd.DataFrame, price_index: pd.DataFrame, date_col: str, val_col: str) -> pd.Series:
    """把事件序列对齐到日频（事件日=值，非事件日=0），返回 multi-index (ts_code, trade_date)。"""
    px = price_index.copy()
    merged = px.merge(
        events[["ts_code", date_col, val_col]].rename(columns={date_col: "trade_date"}),
        on=["ts_code", "trade_date"], how="left",
    )
    merged[val_col] = merged[val_col].fillna(0.0)
    return merged.set_index(["ts_code", "trade_date"])[val_col]


def main(fwd: int):
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price["close"] = pd.to_numeric(price["close"], errors="coerce")
    price = price.dropna(subset=["trade_date", "close"]).sort_values(["ts_code", "trade_date"])
    price_index = price[["ts_code", "trade_date"]]
    c = price.set_index(["ts_code", "trade_date"])["close"]

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    F = {}

    # ── 增减持：过去60日净增减持 ──
    try:
        ht = pd.read_pickle(HT_PKL)
        ht["ann_date"] = pd.to_datetime(ht["ann_date"].astype(str), errors="coerce")
        ht["change_ratio"] = pd.to_numeric(ht["change_ratio"], errors="coerce")
        ht = ht.dropna(subset=["ann_date", "change_ratio"])
        ht["signed"] = np.where(ht["in_de"] == "IN", ht["change_ratio"], -ht["change_ratio"])
        ht_agg = ht.groupby(["ts_code", "ann_date"])["signed"].sum().reset_index()
        daily = _daily_series(ht_agg, price_index, "ann_date", "signed")
        F["增减持净额(60日)"] = daily.groupby(level="ts_code").rolling(60, min_periods=1).sum().reset_index(level=0, drop=True)
    except Exception as e:
        print(f"增减持加载失败: {e}")

    # ── 解禁：未来30日解禁比例 ──
    try:
        sf = pd.read_pickle(SF_PKL)
        sf["float_date"] = pd.to_datetime(sf["float_date"].astype(str), errors="coerce")
        sf["float_ratio"] = pd.to_numeric(sf["float_ratio"], errors="coerce")
        sf = sf.dropna(subset=["float_date", "float_ratio"])
        sf_agg = sf.groupby(["ts_code", "float_date"])["float_ratio"].sum().reset_index()
        daily = _daily_series(sf_agg, price_index, "float_date", "float_ratio")
        cum = daily.groupby(level="ts_code").cumsum()
        F["解禁压力(未来30日)"] = cum.groupby(level="ts_code").shift(-30) - cum
    except Exception as e:
        print(f"解禁加载失败: {e}")

    print(f"\n=== 事件因子横截面 IC (前向{fwd}日) ===")
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
        print(f"  {name:<24} mean IC={mean_ic:+.4f}  t={t:+.2f}  ICIR={icir:+.3f}  IC天数={len(ic)}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
