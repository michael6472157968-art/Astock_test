"""stk_factor_pro 非技术指标字段横截面 IC 扫描。

字段：updays/downdays/lowdays/topdays(连续涨跌天数) + pe_ttm/volume_ratio/turnover_rate_f。
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_pro_extended.py [sample_size] [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.tushare_client import get_pro

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "factor_pro_ext.pkl")

FIELDS = [
    "updays", "downdays", "lowdays", "topdays",
    "pe_ttm", "volume_ratio", "turnover_rate_f",
]


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def load_factor_pro(codes):
    if os.path.exists(OUT):
        return pd.read_pickle(OUT)
    pro = get_pro()
    fields = "ts_code,trade_date,close," + ",".join(FIELDS)
    frames = []
    for i, code in enumerate(codes):
        try:
            d = pro.stk_factor_pro(ts_code=code, start_date="20160101", end_date="20260814", fields=fields)
            if d is not None and not d.empty:
                frames.append(d)
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(codes)}", flush=True)
    out = pd.concat(frames, ignore_index=True)
    out.to_pickle(OUT)
    print(f"完成: {len(out)} 行, {out['ts_code'].nunique()} 股 → {OUT}")
    return out


def main(sample_size: int, fwd: int):
    base = pd.read_pickle(PKL)
    codes = base["ts_code"].unique()[:sample_size].tolist()

    df = load_factor_pro(codes)
    for col in FIELDS + ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])

    c = df["close"]
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== stk_factor_pro 非技术指标横截面 IC (样本{len(codes)}股, 前向{fwd}日) ===")
    results = []
    for name in FIELDS:
        f_rank = cs_rank(df[name])
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        if len(tmp) < 5000:
            print(f"  {name}: 样本不足 {len(tmp)}")
            continue
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        results.append((name, mean_ic, t, mean_ic / std_ic if std_ic > 0 else 0, len(ic)))

    results.sort(key=lambda x: -abs(x[2]))
    print(f"{'字段':<20}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}{'天数':<6}")
    for name, mic, t, icir, nd in results:
        mark = " ★" if abs(t) > 5 else (" *" if abs(t) > 3 else "")
        print(f"{name:<20}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{nd:<6}{mark}")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(sample, fwd)
