"""新挖 4 因子 vs 现有因子 相关性分析（正交性验证）。

新因子：股东户数变化(holder_chg) / 筹码获利盘(winner_rate) / 增减持净额 / 解禁压力
现有因子：F1反转 / F2量价背离 / F3低波动 / F4BP / F5SP / F6DP / F10低换手

指标：时间平均横截面 Spearman。|corr|<0.2 正交（可加入），>0.5 冗余（择一）。

用法: cd backend && PYTHONIOENCODING=utf-8 python new_factor_correlation.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")
HOLDER_PKL = os.path.join(DATA_DIR, "stk_holdernumber.pkl")
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
HT_PKL = os.path.join(DATA_DIR, "stk_holdertrade.pkl")
SF_PKL = os.path.join(DATA_DIR, "share_float.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def _roll(s, d, fn):
    return getattr(s.groupby(level="ts_code").rolling(d), fn)().reset_index(level=0, drop=True)


def ts_std(s, d):
    return _roll(s, d, "std")


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(lambda g: g["a"].rolling(d).cov(g["b"])).reset_index(level=0, drop=True)


def corr_block(new: dict, existing: dict):
    """new 因子 vs existing 因子的时间平均横截面相关。"""
    rows = []
    for nname, nf in new.items():
        row = {}
        for ename, ef in existing.items():
            a, b = cs_rank(nf), cs_rank(ef)
            tmp = pd.DataFrame({"a": a, "b": b}).dropna()
            if len(tmp) < 1000:
                row[ename] = 0.0
                continue
            daily = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
            row[ename] = daily.mean()
        rows.append((nname, row))
    return rows


def main():
    # ── 现有因子 ──
    df = pd.read_pickle(LONG_PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h, v = df["close"], df["high"], df["vol"]
    ret = c.groupby(level="ts_code").pct_change()

    db = pd.read_pickle(BASIC_PKL)
    for col in ["pb", "ps_ttm", "dv_ttm", "turnover_rate"]:
        db[col] = pd.to_numeric(db[col], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    existing = {
        "F1反转": -c.groupby(level="ts_code").pct_change(42),
        "F2量价背离": -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 5)),
        "F3低波动": -ts_std(ret, 21),
        "F4价值BP": 1.0 / db["pb"].replace(0, np.nan),
        "F5价值SP": 1.0 / db["ps_ttm"].replace(0, np.nan),
        "F6价值DP": db["dv_ttm"],
        "F10低换手": -db["turnover_rate"],
    }

    # ── 新因子 ──
    new = {}

    # 1. 股东户数变化（point-in-time）
    hh = pd.read_pickle(HOLDER_PKL)
    hh["ann_date"] = pd.to_datetime(hh["ann_date"].astype(str), errors="coerce")
    hh["end_date"] = pd.to_datetime(hh["end_date"].astype(str), errors="coerce")
    hh["holder_num"] = pd.to_numeric(hh["holder_num"], errors="coerce")
    hh = hh.dropna(subset=["ann_date", "holder_num"]).sort_values(["ts_code", "end_date", "ann_date"])
    hh = hh.drop_duplicates(["ts_code", "end_date"], keep="last").sort_values(["ts_code", "end_date"])
    hh["holder_chg"] = hh.groupby("ts_code")["holder_num"].pct_change()
    hh = hh.dropna(subset=["holder_chg"]).sort_values(["ts_code", "ann_date"])
    hh = hh[["ts_code", "ann_date", "holder_chg"]]
    # merge_asof 到日频
    price_idx = df.index.to_frame().reset_index(drop=True)
    frames = []
    for code, pg in price_idx.groupby("ts_code", sort=False):
        fg = hh[hh["ts_code"] == code]
        if fg.empty:
            continue
        m = pd.merge_asof(pg[["trade_date"]].sort_values("trade_date"), fg[["ann_date", "holder_chg"]].sort_values("ann_date"),
                          left_on="trade_date", right_on="ann_date", direction="backward")
        m["ts_code"] = code
        frames.append(m)
    pit_h = pd.concat(frames, ignore_index=True).set_index(["ts_code", "trade_date"])["holder_chg"]
    new["股东户数变化"] = pit_h

    # 2. 筹码获利盘 winner_rate（日频）
    cq = pd.read_pickle(CYQ_PKL)
    cq["winner_rate"] = pd.to_numeric(cq["winner_rate"], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    cq = cq.dropna(subset=["trade_date", "winner_rate"]).set_index(["ts_code", "trade_date"]).sort_index()
    new["筹码获利盘"] = cq["winner_rate"]

    # 3. 增减持净额（60日）+ 4. 解禁压力（未来30日）
    ht = pd.read_pickle(HT_PKL)
    ht["ann_date"] = pd.to_datetime(ht["ann_date"].astype(str), errors="coerce")
    ht["change_ratio"] = pd.to_numeric(ht["change_ratio"], errors="coerce")
    ht = ht.dropna(subset=["ann_date", "change_ratio"])
    ht["signed"] = np.where(ht["in_de"] == "IN", ht["change_ratio"], -ht["change_ratio"])
    ht_agg = ht.groupby(["ts_code", "ann_date"])["signed"].sum().reset_index()
    ht_agg.columns = ["ts_code", "trade_date", "signed"]
    price_idx2 = df.index.to_frame().reset_index(drop=True)
    mht = price_idx2.merge(ht_agg, on=["ts_code", "trade_date"], how="left")
    mht["signed"] = mht["signed"].fillna(0.0)
    mht = mht.set_index(["ts_code", "trade_date"])["signed"]
    new["增减持净额"] = mht.groupby(level="ts_code").rolling(60, min_periods=1).sum().reset_index(level=0, drop=True)

    sf = pd.read_pickle(SF_PKL)
    sf["float_date"] = pd.to_datetime(sf["float_date"].astype(str), errors="coerce")
    sf["float_ratio"] = pd.to_numeric(sf["float_ratio"], errors="coerce")
    sf = sf.dropna(subset=["float_date", "float_ratio"])
    sf_agg = sf.groupby(["ts_code", "float_date"])["float_ratio"].sum().reset_index()
    sf_agg.columns = ["ts_code", "trade_date", "ratio"]
    msf = price_idx2.merge(sf_agg, on=["ts_code", "trade_date"], how="left")
    msf["ratio"] = msf["ratio"].fillna(0.0)
    msf = msf.set_index(["ts_code", "trade_date"])["ratio"]
    cum = msf.groupby(level="ts_code").cumsum()
    new["解禁压力"] = cum.groupby(level="ts_code").shift(-30) - cum

    # ── 相关性 ──
    rows = corr_block(new, existing)
    print(f"\n=== 新因子 vs 现有因子 相关性（时间平均横截面 Spearman）===\n")
    header = f"{'新因子':<12}" + "".join(f"{e:<12}" for e in existing)
    print(header)
    for nname, row in rows:
        line = f"{nname:<12}"
        for ename in existing:
            v = row[ename]
            mark = "**" if abs(v) > 0.4 else ("*" if abs(v) > 0.2 else " ")
            line += f"{v:+8.3f}{mark} "
        print(line)
    print("\n说明: ** = |corr|>0.4 高相关(冗余), * = >0.2 中相关, 无标记 = 正交(可加入)")


if __name__ == "__main__":
    main()
