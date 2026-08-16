"""因子分域 IC 检验 — 因子在哪些行业/市值段更有效。

产出「因子 × 行业」和「因子 × 市值段」的 IC 矩阵，用于：
1. 诊股「分域提示」：银行股→价值PB重要、科技股→成长重要（数据版映射）
2. 新因子挖掘后的分域验证（同步做，写入因子筛选规则）

方法：每个域（行业/市值段）内，因子 rank vs 前向收益 rank 的 Spearman，时间平均。

数据: long_daily.pkl + daily_basic.pkl + cyq_perf.pkl + stocks表(industry)
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_domain_ic.py [forward_days]
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
CYQ_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")
DB_PATH = os.path.join(DATA_DIR, "stock_analyzer.db")


def _roll(s, d, fn):
    return getattr(s.groupby(level="ts_code").rolling(d), fn)().reset_index(level=0, drop=True)


def main(fwd: int):
    # ── 数据 ──
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

    conn = sqlite3.connect(DB_PATH)
    ind_map = pd.read_sql("SELECT ts_code, industry FROM stocks", conn).set_index("ts_code")["industry"]
    conn.close()

    # ── 因子（good 方向）──
    F = {
        "F1反转": -c.groupby(level="ts_code").pct_change(42),
        "F2量价背离": None,  # 需要 cs_rank + cov，下面单独算
        "F3低波动": -_roll(ret, 21, "std"),
        "F4价值BP": 1.0 / db["pb"].replace(0, np.nan),
        "F5价值SP": 1.0 / db["ps_ttm"].replace(0, np.nan),
        "F6价值DP": db["dv_ttm"],
        "F10低换手": -db["turnover_rate"],
    }
    # F2 量价背离 a101_16
    def cs_rank(s):
        return s.groupby(level="trade_date").rank(pct=True)
    def ts_cov(a, b, d):
        ab = pd.DataFrame({"a": a, "b": b})
        return ab.groupby(level="ts_code").apply(lambda g: g["a"].rolling(d).cov(g["b"])).reset_index(level=0, drop=True)
    F["F2量价背离"] = -cs_rank(ts_cov(cs_rank(h), cs_rank(df["vol"]), 5))

    # 筹码获利盘
    cq = pd.read_pickle(CYQ_PKL)
    cq["winner_rate"] = pd.to_numeric(cq["winner_rate"], errors="coerce")
    cq["trade_date"] = pd.to_datetime(cq["trade_date"].astype(str), errors="coerce")
    cq = cq.dropna(subset=["trade_date", "winner_rate"]).set_index(["ts_code", "trade_date"]).sort_index()
    F["筹码获利盘"] = cq["winner_rate"]

    fwd_ret = c.groupby(level="ts_code").shift(-fwd) / c - 1

    # ── 域变量 ──
    # 行业：保留股票数 >= 40 的行业，其余归"其他"
    ind_counts = ind_map.value_counts()
    keep_inds = set(ind_counts[ind_counts >= 40].index)
    def _ind_of(ts):
        ind = ind_map.get(ts, "")
        return ind if ind in keep_inds else "其他"
    # 市值段
    def _cap_of(mv):
        if mv is None or mv <= 0:
            return None
        if mv < 1_000_000: return "小盘<100亿"
        if mv < 5_000_000: return "中盘100-500亿"
        return "大盘>500亿"

    # ── 分域 IC ──
    def within_group_ic(f, groups, min_n=20):
        """group 是 Series(与 f 同 index)，返回 {group: 时间平均 rank IC}。"""
        panel = pd.DataFrame({"f": f, "r": fwd_ret, "g": groups}).dropna()
        fr = panel.groupby(["g", "trade_date"])["f"].rank(pct=True)
        rr = panel.groupby(["g", "trade_date"])["r"].rank(pct=True)
        panel["fr"], panel["rr"] = fr, rr
        panel = panel.dropna(subset=["fr", "rr"])
        out = {}
        for g, sub in panel.groupby("g"):
            # 每天组内样本数
            sizes = sub.groupby(level="trade_date").size()
            if sizes.median() < min_n:
                continue
            ic = sub.groupby(level="trade_date").apply(lambda x: x["fr"].corr(x["rr"]) if len(x) >= min_n else np.nan)
            ic = ic.dropna()
            if len(ic) >= 30:
                out[g] = ic.mean()
        return out

    # 行业分域 IC
    print(f"\n=== 因子 × 行业 分域 IC (前向{fwd}日, 行业内样本≥{40}股) ===\n")
    ind_series = pd.Series([_ind_of(t) for t in df.index.get_level_values("ts_code")], index=df.index)
    ind_names = sorted(set(ind_series), key=lambda x: (x == "其他", -ind_counts.get(x, 0)))
    mat = {}
    for name, f in F.items():
        d = within_group_ic(f, ind_series)
        mat[name] = d
    # 打印矩阵：行=因子，列=行业
    cols = [n for n in ind_names if any(n in d for d in mat.values())]
    print(f"{'因子':<12}" + "".join(f"{c[:8]:>10}" for c in cols))
    for name in F:
        row = mat[name]
        print(f"{name:<12}" + "".join(f"{row.get(c, np.nan):+10.3f}" if c in row else f"{'':>10}" for c in cols))

    # 市值段分域 IC
    print(f"\n=== 因子 × 市值段 分域 IC (前向{fwd}日) ===\n")
    cap_series = db["total_mv"].map(_cap_of)
    mat2 = {}
    for name, f in F.items():
        mat2[name] = within_group_ic(f, cap_series)
    segs = ["小盘<100亿", "中盘100-500亿", "大盘>500亿"]
    print(f"{'因子':<12}" + "".join(f"{s:>14}" for s in segs))
    for name in F:
        row = mat2[name]
        print(f"{name:<12}" + "".join(f"{row.get(s, np.nan):+14.3f}" for s in segs))


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(fwd)
