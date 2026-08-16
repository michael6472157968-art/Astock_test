"""10 因子相关性矩阵 — 正交 / 正关联组合分析。

计算 9 个横截面因子（F1-F8, F10）的两两相关系数（日频横截面 rank 的 Spearman，时间平均）。
- |corr| < 0.2 → 正交（可组合分散）
- |corr| > 0.5 → 正关联（冗余，组合无增益，需择一）
- corr < 0 → 负关联（对冲，组合增强）

F9 放量见顶是「择时」信号（单股时间序列，非横截面排序），天然正交，不参与矩阵、单列说明。

因子方向统一取「good=越高越好」：
  F1 反转=-return_42d, F2 量价背离=a101_16, F3 低波动=-vol_21d,
  F4 BP=1/pb, F5 SP=1/ps_ttm, F6 DP=dv_ttm,
  F7 现金流=cfps_yoy, F8 成长=dt_netprofit_yoy, F10 低换手=-turnover_rate

数据: long_daily.pkl + daily_basic.pkl + fina_indicator.pkl (均1000股×2016-2026)
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_correlation.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")
FIN_PKL = os.path.join(DATA_DIR, "fina_indicator.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def _roll(s, d, fn):
    return getattr(s.groupby(level="ts_code").rolling(d), fn)().reset_index(level=0, drop=True)


def ts_std(s, d):
    return _roll(s, d, "std")


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).cov(g["b"])
    ).reset_index(level=0, drop=True)


def load_pit_two_fields() -> pd.DataFrame:
    """财务因子 point-in-time 展开到日频，只取 cfps_yoy + dt_netprofit_yoy。"""
    fin = pd.read_pickle(FIN_PKL)
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    fin["ts_code"] = fin["ts_code"].astype("object")
    price["ts_code"] = price["ts_code"].astype("object")
    for col in ["ann_date", "end_date"]:
        fin[col] = pd.to_datetime(fin[col].astype(str), errors="coerce")
    fin = fin.dropna(subset=["ann_date"])
    fin = fin.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(["ts_code", "end_date"], keep="last")
    fin = fin.sort_values(["ts_code", "ann_date", "end_date"]).drop_duplicates(["ts_code", "ann_date"], keep="last")
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price = price.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])
    fin = fin[["ts_code", "ann_date", "cfps_yoy", "dt_netprofit_yoy"]].copy()
    for c in ["cfps_yoy", "dt_netprofit_yoy"]:
        fin[c] = pd.to_numeric(fin[c], errors="coerce")
    fin = fin.sort_values(["ts_code", "ann_date"])
    frames = []
    for code, pg in price.groupby("ts_code", sort=False):
        fg = fin[fin["ts_code"] == code]
        if fg.empty:
            continue
        m = pd.merge_asof(
            pg[["trade_date"]].sort_values("trade_date"),
            fg[["ann_date", "cfps_yoy", "dt_netprofit_yoy"]].sort_values("ann_date"),
            left_on="trade_date", right_on="ann_date", direction="backward",
        )
        m["ts_code"] = code
        frames.append(m)
    merged = pd.concat(frames, ignore_index=True)
    return merged.set_index(["ts_code", "trade_date"])


def main():
    # ── 价格/量因子 ──
    df = pd.read_pickle(LONG_PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = df["close"], df["high"], df["vol"]
    ret = c.groupby(level="ts_code").pct_change()

    F = {}
    F["F1反转"] = -c.groupby(level="ts_code").pct_change(42)
    F["F2量价背离"] = -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 5))
    F["F3低波动"] = -ts_std(ret, 21)

    # ── 价值因子（daily_basic，对齐 long_daily index）──
    db = pd.read_pickle(BASIC_PKL)
    db["ts_code"] = db["ts_code"].astype("object")
    for col in ["pb", "ps_ttm", "dv_ttm", "turnover_rate"]:
        db[col] = pd.to_numeric(db[col], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()
    F["F4价值BP"] = 1.0 / db["pb"].replace(0, np.nan)
    F["F5价值SP"] = 1.0 / db["ps_ttm"].replace(0, np.nan)
    F["F6价值DP"] = db["dv_ttm"]
    F["F10低换手"] = -db["turnover_rate"]

    # ── 财务因子（point-in-time）──
    pit = load_pit_two_fields()
    F["F7现金流"] = pit["cfps_yoy"]
    F["F8成长"] = pit["dt_netprofit_yoy"]

    # ── 对齐 + 横截面 rank ──
    table = pd.DataFrame(F)
    table = table.dropna(how="all")
    rank_df = table.groupby(level="trade_date").rank(pct=True)

    # ── 两两时间平均横截面相关 ──
    names = list(F.keys())
    n = len(names)
    corr_mat = pd.DataFrame(np.zeros((n, n)), index=names, columns=names)
    for i in range(n):
        for j in range(i, n):
            a, b = rank_df[names[i]], rank_df[names[j]]
            tmp = pd.DataFrame({"a": a, "b": b}).dropna()
            if len(tmp) < 1000:
                corr_mat.iloc[i, j] = corr_mat.iloc[j, i] = 0.0
                continue
            # 时间平均的横截面相关系数
            daily = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
            corr_mat.iloc[i, j] = corr_mat.iloc[j, i] = daily.mean()

    print(f"\n=== 9 个横截面因子的相关性矩阵（时间平均横截面 Spearman，2016-2026）===\n")
    print(corr_mat.round(3).to_string())

    # ── 正交/正关联/负关联组合 ──
    print("\n=== 正交组合 (|corr|<0.2, 可分散) ===")
    ortho = []
    for i in range(n):
        for j in range(i + 1, n):
            v = corr_mat.iloc[i, j]
            if abs(v) < 0.2:
                ortho.append((names[i], names[j], v))
    for a, b, v in sorted(ortho, key=lambda x: abs(x[2])):
        print(f"  {a} × {b}: corr={v:+.3f}")

    print("\n=== 正关联组合 (corr>0.5, 冗余需择一) ===")
    pos = []
    for i in range(n):
        for j in range(i + 1, n):
            v = corr_mat.iloc[i, j]
            if v > 0.5:
                pos.append((names[i], names[j], v))
    for a, b, v in sorted(pos, key=lambda x: -x[2]):
        print(f"  {a} × {b}: corr={v:+.3f}")

    print("\n=== 负关联组合 (corr<-0.3, 对冲可增强) ===")
    neg = []
    for i in range(n):
        for j in range(i + 1, n):
            v = corr_mat.iloc[i, j]
            if v < -0.3:
                neg.append((names[i], names[j], v))
    for a, b, v in sorted(neg, key=lambda x: x[2]):
        print(f"  {a} × {b}: corr={v:+.3f}")

    print("\n注: F9 放量见顶是择时信号(单股时间序列)，非横截面排序，天然与所有因子正交。")


if __name__ == "__main__":
    main()
