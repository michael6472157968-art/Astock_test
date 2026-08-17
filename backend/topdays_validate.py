"""topdays(持续顶部天数) 中性化 + 多空回测。low_good(顶部天数少→涨)。"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
EXT = os.path.join(DATA_DIR, "factor_pro_ext.pkl")
DB_PATH = os.path.join(DATA_DIR, "stock_analyzer.db")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    ext = pd.read_pickle(EXT)
    for col in ["topdays", "close"]:
        ext[col] = pd.to_numeric(ext[col], errors="coerce")
    ext["trade_date"] = pd.to_datetime(ext["trade_date"].astype(str), errors="coerce")
    ext = ext.dropna(subset=["trade_date", "close", "topdays"]).set_index(["ts_code", "trade_date"]).sort_index()

    conn = sqlite3.connect(DB_PATH)
    db = pd.read_sql("SELECT ts_code, trade_date, total_mv FROM daily_basic", conn)
    ind_map = pd.read_sql("SELECT ts_code, industry FROM stocks", conn).set_index("ts_code")["industry"]
    conn.close()
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")

    m = ext.merge(db, on=["ts_code", "trade_date"], how="left").set_index(["ts_code", "trade_date"])
    c = m["close"]
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1

    log_mv = np.log(m["total_mv"].replace(0, np.nan))
    panel = pd.DataFrame({"f": m["topdays"], "lm": log_mv, "r": fwd_ret}).dropna()
    ind = ind_map.reindex(panel.index.get_level_values("ts_code")).values
    panel["ind"] = ind
    panel = panel.dropna(subset=["ind"])

    def ic_of(series):
        fr = series.groupby(level="trade_date").rank(pct=True)
        rr = panel["r"].groupby(level="trade_date").rank(pct=True)
        tmp = pd.DataFrame({"a": fr, "b": rr}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"])).dropna()
        return ic.mean(), ic.mean() / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0

    raw_ic, raw_t = ic_of(panel["f"])
    fz = panel["f"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
    panel["fz"] = fz
    f_ind = fz - panel.groupby(["ind", "trade_date"])["fz"].transform("mean")
    lm_z = panel["lm"].groupby(level="trade_date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-12))
    beta = (f_ind * lm_z).groupby(level="trade_date").transform("sum") / (lm_z ** 2).groupby(level="trade_date").transform("sum")
    resid = f_ind - beta * lm_z
    neu_ic, neu_t = ic_of(resid)
    print(f"topdays 中性化(fwd={fwd}): 原始IC={raw_ic:+.4f} t={raw_t:+.1f} | 中性IC={neu_ic:+.4f} t={neu_t:+.1f} | 变化={neu_ic-raw_ic:+.4f}")

    # 多空回测：topdays low_good，做多 q0(天数少) 空 q4(天数多)
    f_inv = 1 - cs_rank(panel["f"])  # 天数少=高分=好
    panel["score"] = f_inv
    panel["year"] = [t.year for t in panel.index.get_level_values("trade_date")]
    panel["q"] = panel.groupby(level="trade_date")["score"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")
    )
    pp = panel.dropna(subset=["q"]).astype({"q": int})
    dates = sorted(pp.index.get_level_values("trade_date").unique())
    periods_per_year = 252 / fwd
    rows = []
    for offset in range(fwd):
        for rd in dates[offset::fwd]:
            try:
                day = pp.xs(rd, level="trade_date")
            except KeyError:
                continue
            q0 = day[day["q"] == 0]
            q4 = day[day["q"] == 4]
            if len(q0) < 3 or len(q4) < 3:
                continue
            ls = q0["r"].mean() - q4["r"].mean()  # 做多天数少 空天数多
            rows.append((day["year"].iloc[0], ls))
    r = pd.DataFrame(rows, columns=["year", "ls"])
    gross = r["ls"].mean() * periods_per_year
    sharpe = r["ls"].mean() / r["ls"].std() * np.sqrt(periods_per_year) if r["ls"].std() > 0 else 0
    pos_years = (r.groupby("year")["ls"].mean() > 0).sum()
    n_years = r["year"].nunique()
    print(f"topdays 多空(持有{fwd}天): 毛年化={gross*100:+.2f}% 夏普={sharpe:.2f} 正年份={pos_years}/{n_years} 周期数={len(r)}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(fwd)
