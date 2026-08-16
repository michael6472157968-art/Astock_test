"""融资买入占比(rzmre/amount) 正交性 + 中性化验证（全市场 DB 数据源）。"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stock_analyzer.db")


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def main(fwd: int):
    conn = sqlite3.connect(DB_PATH)
    mr = pd.read_sql("SELECT trade_date, ts_code, rzmre FROM margin_records", conn)
    sd = pd.read_sql("SELECT trade_date, ts_code, close, amount FROM stock_daily", conn)
    db = pd.read_sql("SELECT trade_date, ts_code, pb, turnover_rate, total_mv FROM daily_basic", conn)
    ind_map = pd.read_sql("SELECT ts_code, industry FROM stocks", conn).set_index("ts_code")["industry"]
    conn.close()

    for df in [mr, sd, db]:
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")

    # 主面板
    m = mr.merge(sd[["trade_date", "ts_code", "close", "amount"]], on=["trade_date", "ts_code"], how="left")
    m = m.merge(db[["trade_date", "ts_code", "pb", "turnover_rate", "total_mv"]], on=["trade_date", "ts_code"], how="left")
    m = m.dropna(subset=["close", "amount"]).sort_values(["ts_code", "trade_date"]).set_index(["ts_code", "trade_date"])
    rzmre_ratio = m["rzmre"] / m["amount"].replace(0, np.nan)
    c = m["close"]

    # 现有因子（用 stock_daily 的 close 算反转）
    rev42 = -c.groupby(level="ts_code").pct_change(42)
    existing = {
        "反转42d": rev42,
        "低换手": -m["turnover_rate"],
        "换手率": m["turnover_rate"],
        "价值BP": 1.0 / m["pb"].replace(0, np.nan),
    }

    print("=== 融资买入占比 正交性(时间平均横截面Spearman) ===")
    a = cs_rank(rzmre_ratio)
    for name, f in existing.items():
        b = cs_rank(f)
        tmp = pd.DataFrame({"a": a, "b": b}).dropna()
        if len(tmp) < 1000:
            continue
        daily = tmp.groupby(level="trade_date").apply(lambda g: g["a"].corr(g["b"]))
        mark = "  <- 高相关" if abs(daily.mean()) > 0.4 else ("  <- 中相关" if abs(daily.mean()) > 0.2 else "")
        print(f"  {name:<12} corr={daily.mean():+.3f}{mark}")

    # 中性化
    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1
    log_mv = np.log(m["total_mv"].replace(0, np.nan))
    panel = pd.DataFrame({"f": rzmre_ratio, "lm": log_mv, "r": fwd_ret}).dropna()
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

    print(f"\n=== 融资买入占比 中性化 (fwd={fwd}) ===")
    print(f"  原始IC={raw_ic:+.4f} t={raw_t:+.1f} | 中性IC={neu_ic:+.4f} t={neu_t:+.1f} | 变化={neu_ic-raw_ic:+.4f}")
    print(f"  -> {'✅ 真alpha' if abs(neu_t) > 5 else '⚠️ 市值/行业beta占比高'}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
