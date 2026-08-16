"""因子中性化检验 — 行业+市值中性化前后的 IC 对比。

目的：识别哪些因子是「真 alpha」，哪些是「市值/行业 beta」的代理。

对每个因子：
1. 原始 RankIC（因子 rank vs 前向收益 rank）
2. 中性化 RankIC（因子 z-score → 行业 de-mean → 对 log市值回归取残差 → rank）

若中性化后 IC 大幅下降 → 该因子混入了市值/行业 beta，需重新评估。

数据: long_daily.pkl + daily_basic.pkl + stocks表(industry)
用法: cd backend && PYTHONIOENCODING=utf-8 python neutralization_test.py [forward_days]
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
BASIC_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")
DB_PATH = os.path.join(DATA_DIR, "stock_analyzer.db")


def _roll(s, d, fn):
    return getattr(s.groupby(level="ts_code").rolling(d), fn)().reset_index(level=0, drop=True)


def main(fwd: int):
    # ── 价格 + 估值 ──
    df = pd.read_pickle(LONG_PKL)
    for col in ["high", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    df = df.dropna(subset=["close", "trade_date"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    c, h = df["close"], df["high"]
    ret = c.groupby(level="ts_code").pct_change()

    db = pd.read_pickle(BASIC_PKL)
    for col in ["pb", "ps_ttm", "dv_ttm", "turnover_rate", "total_mv"]:
        db[col] = pd.to_numeric(db[col], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    db = db.dropna(subset=["trade_date"]).set_index(["ts_code", "trade_date"]).sort_index()

    # ── 行业映射（静态）──
    conn = sqlite3.connect(DB_PATH)
    ind_map = pd.read_sql("SELECT ts_code, industry FROM stocks", conn).set_index("ts_code")["industry"]
    conn.close()

    # ── 因子 ──
    F = {
        "F1反转": -c.groupby(level="ts_code").pct_change(42),
        "F3低波动": -_roll(ret, 21, "std"),
        "F4价值BP": 1.0 / db["pb"].replace(0, np.nan),
        "F5价值SP": 1.0 / db["ps_ttm"].replace(0, np.nan),
        "F6价值DP": db["dv_ttm"],
        "F10低换手": -db["turnover_rate"],
    }

    log_mv = np.log(db["total_mv"].replace(0, np.nan))
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1

    print(f"\n=== 因子中性化检验 (行业+市值, 前向{fwd}日) ===")
    print(f"{'因子':<12}{'原始IC':<10}{'原始t':<10}{'中性IC':<10}{'中性t':<10}{'IC变化':<10}")

    for name, f in F.items():
        panel = pd.DataFrame({"f": f, "lm": log_mv, "r": fwd_ret}).dropna()
        if len(panel) < 5000:
            print(f"  {name}: 样本不足")
            continue
        ind = ind_map.reindex(panel.index.get_level_values("ts_code")).values
        panel["ind"] = ind

        # 原始 RankIC
        def ic_of(series, ret_col="r"):
            fr = series.groupby(level="trade_date").rank(pct=True)
            rr = panel[ret_col].groupby(level="trade_date").rank(pct=True)
            tmp = pd.DataFrame({"a": fr, "b": rr}).dropna()
            ic = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
            ic = ic.dropna()
            return ic.mean(), ic.mean() / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0

        raw_ic, raw_t = ic_of(panel["f"])

        # 中性化：z-score → 行业de-mean → 市值回归残差
        fz = panel["f"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
        f_ind = fz - panel.groupby(["ind", "trade_date"])["f"].transform("mean")
        # 用 fz 的行业均值替代（上面 groupby 需要 f 在 panel 里，直接算）
        panel["fz"] = fz
        f_ind = fz - panel.groupby(["ind", "trade_date"])["fz"].transform("mean")
        lm_z = panel["lm"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
        beta = (f_ind * lm_z).groupby(level="trade_date").transform("sum") / (lm_z ** 2).groupby(level="trade_date").transform("sum")
        resid = f_ind - beta * lm_z
        panel["resid"] = resid

        neu_ic, neu_t = ic_of(panel["resid"])

        diff = neu_ic - raw_ic
        print(f"{name:<12}{raw_ic:+.4f}{raw_t:>8.1f}{neu_ic:>10.4f}{neu_t:>8.1f}{diff:>+9.4f}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
