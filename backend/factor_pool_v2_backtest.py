"""因子选股池 v2 方案对比回测。

对比选股池组合方案（横截面多空 top20% vs bottom20%，5档，持有N天）：
  1. baseline  当前线上版：量价背离F2 + 成长F8 + 现金流F7 等权
  2. 强因子IC加权：F9低换手 + F4价值BP + F1反转 + F2量价背离 + F11筹码宽度（|IC|加权，去冗余后）
  3. 强因子 + 正交补充：方案2 + F7/F8 各 5% 分散化
  4. 方案3 + 市值分层：市值3档内多空等权（市值中性，最接近真实可投策略）

数据: 10年 pkl（long_daily/daily_basic/fina_indicator/cyq_perf/stk_holdernumber）
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_pool_v2_backtest.py [hold_days] [cost_bp]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 因子定义：(方向 low_good/high_good, |IC| 用于IC加权)
# IC 值来自 factor_meta.json（已挖掘收敛，不重新验证）
FACTORS = {
    "f1_rev42":     ("low",  0.056),
    "f2_corr20":    ("low",  0.056),
    "f3_vol21":     ("low",  0.069),
    "f4_bp":        ("low",  0.061),
    "f5_sp":        ("low",  0.047),
    "f6_dp":        ("high", 0.049),
    "f7_cfps":      ("high", 0.0135),
    "f8_growth":    ("high", 0.0153),
    "f9_turnover":  ("low",  0.109),
    "f10_holder":   ("low",  0.020),
    "f11_conc":     ("high", 0.038),
}


def load_data() -> pd.DataFrame:
    """加载并合并所有 pkl 到统一长表 (ts_code, trade_date, ...)。"""
    long = pd.read_pickle(os.path.join(DATA_DIR, "long_daily.pkl"))
    long["trade_date"] = pd.to_datetime(long["trade_date"].astype(str), errors="coerce")
    long = long.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"])
    for c in ["close", "vol", "amount"]:
        long[c] = pd.to_numeric(long[c], errors="coerce")

    db = pd.read_pickle(os.path.join(DATA_DIR, "daily_basic.pkl"))
    db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str), errors="coerce")
    for c in ["pb", "ps_ttm", "dv_ttm", "turnover_rate", "circ_mv", "total_mv"]:
        db[c] = pd.to_numeric(db[c], errors="coerce")
    db = db.dropna(subset=["trade_date"])

    cyq = pd.read_pickle(os.path.join(DATA_DIR, "cyq_perf.pkl"))
    cyq["trade_date"] = pd.to_datetime(cyq["trade_date"].astype(str), errors="coerce")
    for c in ["cost_5pct", "cost_95pct", "weight_avg"]:
        cyq[c] = pd.to_numeric(cyq[c], errors="coerce")
    cyq = cyq.dropna(subset=["trade_date"])

    # 主表：日线 + 估值 + 筹码（同 (ts_code, trade_date) 直接 merge）
    df = long.merge(
        db[["ts_code", "trade_date", "pb", "ps_ttm", "dv_ttm",
            "turnover_rate", "circ_mv", "total_mv"]],
        on=["ts_code", "trade_date"], how="left",
    ).merge(
        cyq[["ts_code", "trade_date", "cost_5pct", "cost_95pct", "weight_avg"]],
        on=["ts_code", "trade_date"], how="left",
    )

    # 财务 F7/F8：point-in-time 映射（end_date <= trade_date 的最新一条）
    fina = pd.read_pickle(os.path.join(DATA_DIR, "fina_indicator.pkl"))
    fina["end_date"] = pd.to_datetime(fina["end_date"].astype(str), errors="coerce")
    for c in ["cfps_yoy", "dt_netprofit_yoy"]:
        fina[c] = pd.to_numeric(fina[c], errors="coerce")
    fina = fina.dropna(subset=["end_date"]).sort_values("end_date")
    df = pd.merge_asof(
        df.sort_values("trade_date"),
        fina[["ts_code", "end_date", "cfps_yoy", "dt_netprofit_yoy"]],
        left_on="trade_date", right_on="end_date", by="ts_code", direction="backward",
    )

    # 股东户数 F10：point-in-time 映射后算环比
    holder = pd.read_pickle(os.path.join(DATA_DIR, "stk_holdernumber.pkl"))
    holder["end_date"] = pd.to_datetime(holder["end_date"].astype(str), errors="coerce")
    holder["holder_num"] = pd.to_numeric(holder["holder_num"], errors="coerce")
    holder = holder.dropna(subset=["end_date", "holder_num"]).sort_values("end_date")
    df = pd.merge_asof(
        df.sort_values("trade_date"),
        holder[["ts_code", "end_date", "holder_num"]],
        left_on="trade_date", right_on="end_date", by="ts_code", direction="backward",
    )

    return df


def build_factor_values(df: pd.DataFrame) -> pd.DataFrame:
    """计算各因子原始值（未 rank 前），追加到 df。"""
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    g = df.groupby("ts_code")

    # F1 反转 return_42d（low_good：跌得多=好）
    df["f1_rev42"] = g["close"].pct_change(42)

    # F3 低波动 vol_21d（low_good）
    df["ret1"] = g["close"].pct_change()
    df["f3_vol21"] = g["ret1"].transform(lambda s: s.rolling(21, min_periods=15).std())

    # F2 量价背离：20日 (close, vol) 相关系数（low_good：负相关=缩量涨健康）
    df["vol_chg"] = g["vol"].pct_change()
    df["f2_corr20"] = (
        df.groupby("ts_code")[["ret1", "vol_chg"]]
        .apply(lambda x: x["ret1"].rolling(20, min_periods=10).corr(x["vol_chg"]))
        .reset_index(level=0, drop=True)
    )

    # F4/F5/F6/F9 直接来自 daily_basic
    df["f4_bp"] = 1.0 / df["pb"]
    df["f5_sp"] = 1.0 / df["ps_ttm"]
    df["f6_dp"] = df["dv_ttm"]
    df["f9_turnover"] = df["turnover_rate"]

    # F7/F8 财务（merge_asof 已带入）
    df["f7_cfps"] = df["cfps_yoy"]
    df["f8_growth"] = df["dt_netprofit_yoy"]

    # F10 股东户数变化（low_good：户数减少=筹码集中）
    df["holder_prev"] = g["holder_num"].shift(1)
    df["f10_holder"] = (df["holder_num"] - df["holder_prev"]) / df["holder_prev"].replace(0, np.nan)

    # F11 筹码宽度 concentration = (cost_95 - cost_5)/weight_avg（high_good）
    df["f11_conc"] = (df["cost_95pct"] - df["cost_5pct"]) / df["weight_avg"].replace(0, np.nan)

    return df


def cs_rank_to_score(df: pd.DataFrame) -> pd.DataFrame:
    """把各因子转成横截面 [0,1] 分位，统一方向为「越高越好」。"""
    for f, (direction, _) in FACTORS.items():
        if f not in df.columns:
            continue
        r = df.groupby("trade_date")[f].rank(pct=True)
        df[f + "_score"] = 1 - r if direction == "low" else r
    return df


def ic_weight(names: list[str]) -> dict:
    """按 |IC| 归一化得到权重（因子名 -> 权重）。"""
    tot = sum(FACTORS[n][1] for n in names)
    return {n: FACTORS[n][1] / tot for n in names}


def make_score(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    score = sum(df[f + "_score"] * w for f, w in weights.items())
    return df.assign(score=score)


def backtest_ls(df: pd.DataFrame, hold_days: int) -> tuple:
    """全市场多空（top20% vs bottom20%，5档），返回 (gross, sharpe, pos_years, n_years, n_periods)。"""
    df = df.copy()
    df["fwd_ret"] = df.groupby("ts_code")["close"].shift(-hold_days) / df["close"] - 1
    df["year"] = df["trade_date"].dt.year
    m = df.dropna(subset=["score", "fwd_ret"])
    if len(m) < 100:
        return 0.0, 0.0, 0, 0, 0
    m = m.assign(q=m.groupby("trade_date")["score"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")))
    m = m.dropna(subset=["q"]).astype({"q": int})

    dates = sorted(m["trade_date"].unique())
    periods_per_year = 252 / hold_days
    rows = []
    for offset in range(hold_days):
        for rd in dates[offset::hold_days]:
            day = m[m["trade_date"] == rd]
            q0 = day[day["q"] == 0]
            q4 = day[day["q"] == 4]
            if len(q0) < 3 or len(q4) < 3:
                continue
            ls = q4["fwd_ret"].mean() - q0["fwd_ret"].mean()
            rows.append((day["year"].iloc[0], ls))
    if not rows:
        return 0.0, 0.0, 0, 0, 0
    r = pd.DataFrame(rows, columns=["year", "ls"])
    gross = r["ls"].mean() * periods_per_year
    sharpe = r["ls"].mean() / r["ls"].std() * np.sqrt(periods_per_year) if r["ls"].std() > 0 else 0
    pos = int((r.groupby("year")["ls"].mean() > 0).sum())
    ny = int(r["year"].nunique())
    return gross, sharpe, pos, ny, len(r)


def backtest_ls_mv_neutral(df: pd.DataFrame, hold_days: int) -> tuple:
    """市值 3 档内多空等权（市值中性），返回 (gross, sharpe, pos_years, n_years)。"""
    df = df.copy()
    df["fwd_ret"] = df.groupby("ts_code")["close"].shift(-hold_days) / df["close"] - 1
    df["year"] = df["trade_date"].dt.year
    m = df.dropna(subset=["score", "fwd_ret", "total_mv"])
    if len(m) < 300:
        return 0.0, 0.0, 0, 0
    m = m.assign(mv_q=m.groupby("trade_date")["total_mv"].transform(
        lambda x: pd.qcut(x, 3, labels=False, duplicates="drop")))
    m = m.dropna(subset=["mv_q"])
    grosses, sharpes, pos_list, ny_list = [], [], [], []
    for g in [0, 1, 2]:
        sub = m[m["mv_q"] == g]
        gr, sh, pos, ny, _ = backtest_ls(sub, hold_days)
        grosses.append(gr)
        sharpes.append(sh)
        pos_list.append(pos)
        ny_list.append(ny)
    gross = np.mean(grosses)
    sharpe = np.mean(sharpes)
    pos = int(np.mean(pos_list))
    ny = int(np.mean(ny_list))
    return gross, sharpe, pos, ny


def backtest_long_only(df: pd.DataFrame, hold_days: int, top_n: int = 10) -> tuple:
    """纯多头：每日选得分 top_n 只等权持有 N 天，对比全市场等权基准。
    返回 (top年化, 基准年化, 超额年化, top夏普, 正超额年份, 周期数)。"""
    df = df.copy()
    df["fwd_ret"] = df.groupby("ts_code")["close"].shift(-hold_days) / df["close"] - 1
    df["year"] = df["trade_date"].dt.year
    m = df.dropna(subset=["score", "fwd_ret"])
    if len(m) < 100:
        return 0.0, 0.0, 0.0, 0.0, 0, 0
    dates = sorted(m["trade_date"].unique())
    mkt = m.groupby("trade_date")["fwd_ret"].mean()
    periods_per_year = 252 / hold_days
    rows = []
    for offset in range(hold_days):
        for rd in dates[offset::hold_days]:
            day = m[m["trade_date"] == rd]
            if len(day) < top_n:
                continue
            top = day.sort_values("score", ascending=False).head(top_n)
            rows.append((day["year"].iloc[0], top["fwd_ret"].mean(), mkt.get(rd, np.nan)))
    if not rows:
        return 0.0, 0.0, 0.0, 0.0, 0, 0
    r = pd.DataFrame(rows, columns=["year", "top", "mkt"])
    top_ann = r["top"].mean() * periods_per_year
    mkt_ann = r["mkt"].mean() * periods_per_year
    excess = (r["top"] - r["mkt"]).mean() * periods_per_year
    excess_s = (r["top"] - r["mkt"]).std()
    sharpe = excess / excess_s * np.sqrt(periods_per_year) if excess_s > 0 else 0
    pos = int((r.groupby("year")["top"].mean() > r.groupby("year")["mkt"].mean()).sum())
    ny = int(r["year"].nunique())
    return top_ann, mkt_ann, excess, sharpe, pos, ny


def main(hold_days: int, cost_bp: int):
    print("加载 10 年 pkl 并计算因子...")
    df = load_data()
    df = build_factor_values(df)
    df = cs_rank_to_score(df)
    n_stocks = df["ts_code"].nunique()
    print(f"数据：{len(df)} 行，{n_stocks} 只股票\n")

    strong = ["f9_turnover", "f4_bp", "f1_rev42", "f2_corr20", "f11_conc"]
    w_strong = ic_weight(strong)
    # 方案3：强因子 90% + F7/F8 各 5%
    w_ortho = {k: v * 0.90 for k, v in w_strong.items()}
    w_ortho["f7_cfps"] = 0.05
    w_ortho["f8_growth"] = 0.05

    SCHEMES = {
        "1.baseline F2+F8+F7等权": {"f2_corr20": 1 / 3, "f8_growth": 1 / 3, "f7_cfps": 1 / 3},
        "2.强因子IC加权(F9/F4/F1/F2/F11)": w_strong,
        "3.强因子+正交补充F7F8": w_ortho,
        "5.F2/F8/F7 IC加权": {"f2_corr20": 0.66, "f8_growth": 0.18, "f7_cfps": 0.16},
        "6.F2/F8/F7/F10股东户数等权": {"f2_corr20": 0.25, "f8_growth": 0.25, "f7_cfps": 0.25, "f10_holder": 0.25},
    }

    print(f"=== 因子选股池方案对比 (多空 top20% vs bottom20%, 持有{hold_days}天, {n_stocks}股×10年) ===")
    print(f"{'方案':<34}{'毛年化':<10}{'夏普':<8}{'正年份':<10}{'周期数':<8}")
    results = {}
    for sname, w in SCHEMES.items():
        d = make_score(df, w)
        gross, sharpe, pos, ny, np_ = backtest_ls(d, hold_days)
        results[sname] = (gross, sharpe, pos, ny)
        print(f"{sname:<34}{gross*100:+8.2f}%  {sharpe:<8.2f}{f'{pos}/{ny}':<10}{np_:<8}")

    # 方案4：市值中性
    d = make_score(df, w_ortho)
    gross, sharpe, pos, ny = backtest_ls_mv_neutral(d, hold_days)
    results["4.方案3+市值3档中性"] = (gross, sharpe, pos, ny)
    print(f"{'4.方案3+市值3档中性':<34}{gross*100:+8.2f}%  {sharpe:<8.2f}{f'{pos}/{ny}':<10}{'-':<8}")

    print("\n注：毛年化未扣成本（多空换手近似抵消），市值中性档为3档等权平均。")

    print(f"\n=== 纯多头口径（选{10}只等权 vs 全市场等权，持有{hold_days}天）——选股池真实场景 ===")
    print(f"{'方案':<34}{'组合年化':<10}{'基准年化':<10}{'超额年化':<10}{'超额夏普':<9}{'正超额年份':<10}")
    for sname, w in list(SCHEMES.items()) + [("4.方案3+市值3档中性", w_ortho)]:
        d = make_score(df, w)
        top_ann, mkt_ann, excess, sharpe, pos, ny = backtest_long_only(d, hold_days, top_n=10)
        print(f"{sname:<34}{top_ann*100:+8.2f}%  {mkt_ann*100:+8.2f}%  {excess*100:+8.2f}%  {sharpe:<9.2f}{f'{pos}/{ny}':<10}")


if __name__ == "__main__":
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cost = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    main(hold, cost)
