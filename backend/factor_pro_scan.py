"""stk_factor_pro 技术指标横截面 IC 扫描。

时间序列已证伪「振荡器无 alpha」，但横截面未测。本脚本拉 500 股 × 10 年
stk_factor_pro 的 57 个技术指标（_bfq 不复权），横截面 IC 检验，找有 alpha 的指标。

用法: cd backend && PYTHONIOENCODING=utf-8 python factor_pro_scan.py [sample_size] [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.tushare_client import get_pro

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "factor_pro_data.pkl")

# 57 个 _bfq 技术指标字段
BFQ_FIELDS = [
    "asi_bfq", "asit_bfq", "atr_bfq", "bbi_bfq", "bias1_bfq", "bias2_bfq", "bias3_bfq",
    "boll_lower_bfq", "boll_mid_bfq", "boll_upper_bfq", "brar_ar_bfq", "brar_br_bfq",
    "cci_bfq", "cr_bfq", "dfma_dif_bfq", "dfma_difma_bfq", "dmi_adx_bfq", "dmi_adxr_bfq",
    "dmi_mdi_bfq", "dmi_pdi_bfq", "dpo_bfq", "madpo_bfq", "emv_bfq", "maemv_bfq",
    "expma_12_bfq", "expma_50_bfq", "kdj_bfq", "kdj_d_bfq", "kdj_k_bfq", "ktn_down_bfq",
    "ktn_mid_bfq", "ktn_upper_bfq", "macd_bfq", "macd_dea_bfq", "macd_dif_bfq",
    "mass_bfq", "ma_mass_bfq", "mfi_bfq", "mtm_bfq", "mtmma_bfq", "obv_bfq",
    "psy_bfq", "psyma_bfq", "roc_bfq", "maroc_bfq", "taq_down_bfq", "taq_mid_bfq",
    "taq_up_bfq", "trix_bfq", "trma_bfq", "vr_bfq", "wr_bfq", "wr1_bfq",
    "xsii_td1_bfq", "xsii_td2_bfq", "xsii_td3_bfq", "xsii_td4_bfq",
]


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def load_factor_pro(codes):
    if os.path.exists(OUT):
        return pd.read_pickle(OUT)
    pro = get_pro()
    fields = "ts_code,trade_date,close," + ",".join(BFQ_FIELDS)
    frames = []
    for i, code in enumerate(codes):
        try:
            d = pro.stk_factor_pro(ts_code=code, start_date="20160101", end_date="20260814", fields=fields)
            if d is not None and not d.empty:
                frames.append(d)
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(codes)}")
    out = pd.concat(frames, ignore_index=True)
    out.to_pickle(OUT)
    print(f"完成: {len(out)} 行, {out['ts_code'].nunique()} 股 → {OUT}")
    return out


def main(sample_size: int, fwd: int):
    base = pd.read_pickle(PKL)
    codes = base["ts_code"].unique()[:sample_size].tolist()

    df = load_factor_pro(codes)
    for col in BFQ_FIELDS + ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])

    c = df["close"]
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== stk_factor_pro 技术指标横截面 IC (样本{len(codes)}股, 前向{fwd}日) ===")
    results = []
    for name in BFQ_FIELDS:
        f_rank = cs_rank(df[name])
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        results.append((name, mean_ic, t, mean_ic / std_ic if std_ic > 0 else 0))

    results.sort(key=lambda x: -abs(x[2]))
    print(f"{'指标':<20}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}")
    for name, mic, t, icir in results:
        mark = " ★" if abs(t) > 5 else ""
        print(f"{name:<20}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{mark}")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(sample, fwd)
