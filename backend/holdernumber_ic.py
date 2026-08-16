"""股东户数变化因子（筹码集中度）横截面 IC 检验 — point-in-time。

因子：户数变化率 = (本期户数 - 上期户数) / 上期户数。
理论：户数下降 = 筹码集中 = 主力吸筹 = 看涨 → 预期「户数变化率」负 IC。

point-in-time：股东户数用 ann_date 公布，交易日 t 只用 ann_date <= t 的最近一期变化，
避免未来函数。户数变化是慢变量（季度频），前向窗口取 20/60 日。

数据：data/stk_holdernumber.pkl + data/long_daily.pkl（价格算 forward return）
用法: cd backend && PYTHONIOENCODING=utf-8 python holdernumber_ic.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HOLDER_PKL = os.path.join(DATA_DIR, "stk_holdernumber.pkl")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def load_pit_holder_change() -> pd.DataFrame:
    """股东户数变化率 point-in-time 展开到日频，返回 multi-index (ts_code, trade_date)。"""
    h = pd.read_pickle(HOLDER_PKL)
    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    h["ts_code"] = h["ts_code"].astype("object")
    price["ts_code"] = price["ts_code"].astype("object")
    for col in ["ann_date", "end_date"]:
        h[col] = pd.to_datetime(h[col].astype(str), errors="coerce")
    h = h.dropna(subset=["ann_date", "end_date"])
    h["holder_num"] = pd.to_numeric(h["holder_num"], errors="coerce")
    h = h.dropna(subset=["holder_num"])
    # 同 (ts_code, end_date) 去重取最后
    h = h.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(["ts_code", "end_date"], keep="last")
    # 户数变化率（相对上一期）
    h = h.sort_values(["ts_code", "end_date"])
    h["holder_chg"] = h.groupby("ts_code")["holder_num"].pct_change()
    h = h.dropna(subset=["holder_chg"])

    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price = price.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])

    h = h.sort_values(["ts_code", "ann_date"])
    right = h[["ts_code", "ann_date", "holder_chg", "holder_num"]]
    frames = []
    for code, pg in price.groupby("ts_code", sort=False):
        fg = right[right["ts_code"] == code]
        if fg.empty:
            continue
        m = pd.merge_asof(
            pg[["trade_date"]].sort_values("trade_date"),
            fg[["ann_date", "holder_chg", "holder_num"]].sort_values("ann_date"),
            left_on="trade_date", right_on="ann_date", direction="backward",
        )
        m["ts_code"] = code
        frames.append(m)
    merged = pd.concat(frames, ignore_index=True)
    return merged.set_index(["ts_code", "trade_date"])


def main(fwd: int):
    pit = load_pit_holder_change()
    print(f"point-in-time 样本: {len(pit)} 行, {pit['holder_chg'].notna().sum()} 有户数变化值")

    price = pd.read_pickle(LONG_PKL)[["ts_code", "trade_date", "close"]]
    price["ts_code"] = price["ts_code"].astype("object")
    price["trade_date"] = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    price = price.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c = price["close"]

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    # 因子：户数变化率（原始方向）+ 筹码集中度（取反）
    factors = {
        "holder_chg(户数变化率)": pit["holder_chg"],           # 预期负IC
        "concentration(-户数变化)": -pit["holder_chg"],        # 预期正IC
    }

    print(f"\n=== 股东户数因子横截面 IC (point-in-time, 前向{fwd}日) ===")
    for name, f in factors.items():
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        if len(tmp) < 5000:
            print(f"  {name}: 有效样本不足 ({len(tmp)}), 跳过")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        print(f"  {name:<26} mean IC={mean_ic:+.4f}  t={t:+.2f}  ICIR={icir:+.3f}  IC天数={len(ic)}")

    # 分年度稳定性（户数变化率）
    tmp = pd.DataFrame({"f": cs_rank(pit["holder_chg"]), "r": r_rank}).dropna()
    ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"])).dropna()
    yearly = ic.groupby(lambda d: str(d)[:4]).mean()
    print(f"\n户数变化率分年度 mean IC:")
    print("  " + " ".join(f"{y[-2:]}:{v:+.3f}" for y, v in yearly.items()))


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
