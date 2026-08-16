"""Tushare 因子库反推验证 — 横截面 IC 检验。

Tushare 8000 积分因子库 202 个因子（9类）。本脚本反推「纯量价」可复现的因子
（Alpha101 / Momentum / Reversal / Risk），用横截面 Rank IC 检验有效性。

数据：stock_daily 日线（open/high/low/close/volume/amount），VWAP = amount/volume。
标签：未来 N 日收益（横截面天然标准化）。
IC = 每日 cs_rank(factor) 与 cs_rank(future_ret) 的 Spearman 相关，输出均值 + t 值。

用法: cd backend && PYTHONIOENCODING=utf-8 python factor_library_test.py [sample_size] [forward_days]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

DB = "data/stock_analyzer.db"


# ── 算子（multi-index: (ts_code, trade_date)）──
def ts_delta(s: pd.Series, d: int) -> pd.Series:
    return s.groupby(level="ts_code").diff(d)


def _roll(s: pd.Series, d: int, fn: str) -> pd.Series:
    r = getattr(s.groupby(level="ts_code").rolling(d), fn)()
    return r.reset_index(level=0, drop=True)


def ts_sum(s, d): return _roll(s, d, "sum")
def ts_mean(s, d): return _roll(s, d, "mean")
def ts_std(s, d): return _roll(s, d, "std")
def ts_max(s, d): return _roll(s, d, "max")
def ts_min(s, d): return _roll(s, d, "min")


def ts_ema(s: pd.Series, span: int) -> pd.Series:
    return s.groupby(level="ts_code").transform(lambda x: x.ewm(span=span, adjust=False).mean())


def cs_rank(s: pd.Series) -> pd.Series:
    """横截面 rank（按 trade_date 分组，pct 归一化 0~1）。"""
    return s.groupby(level="trade_date").rank(pct=True)


def shift_fwd(s: pd.Series, d: int) -> pd.Series:
    return s.groupby(level="ts_code").shift(-d)


def main(sample_size: int, fwd: int):
    conn = sqlite3.connect(DB)
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

    codes = pd.read_sql(
        "SELECT ts_code FROM stocks WHERE list_date < ? AND name NOT LIKE '%ST%' "
        "ORDER BY RANDOM() LIMIT ?",
        conn, params=[year_ago, sample_size * 2],
    )["ts_code"].tolist()[:sample_size]

    placeholders = ",".join("?" for _ in codes)
    df = pd.read_sql(
        f"SELECT ts_code, trade_date, open, high, low, close, volume, amount "
        f"FROM stock_daily WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date",
        conn, params=codes,
    )
    conn.close()

    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    o, h, l, c, v, amt = df["open"], df["high"], df["low"], df["close"], df["volume"], df["amount"]
    ret = c.groupby(level="ts_code").pct_change()
    vwap = (amt / v.replace(0, np.nan)).fillna(c)

    # ── 因子（12 个）──
    factors: dict[str, pd.Series] = {}
    factors["return_5d"] = c.groupby(level="ts_code").pct_change(5)
    factors["return_21d"] = c.groupby(level="ts_code").pct_change(21)

    gain = ret.clip(lower=0)
    loss = (-ret).clip(lower=0)
    rs = ts_ema(gain, 14) / ts_ema(loss, 14).replace(0, np.nan)
    factors["RSI_14"] = 100 - 100 / (1 + rs)

    factors["alpha101_33"] = cs_rank(-(1 - o / c))
    factors["alpha101_101"] = (c - o) / (h - l + 0.001)
    factors["alpha101_12"] = np.sign(ts_delta(v, 1)) * (-ts_delta(c, 1))
    factors["alpha101_41"] = np.sqrt(h * l) - vwap
    factors["alpha101_23"] = (-ts_delta(h, 2)).where(ts_sum(h, 20) / 20 < h, 0.0)

    factors["return_std_21d"] = ts_std(ret, 21)
    factors["sharpe_60d"] = ts_mean(ret, 60) / ts_std(ret, 60)

    dif = ts_ema(c, 12) - ts_ema(c, 26)
    dea = ts_ema(dif, 9)
    factors["MACD"] = 2 * (dif - dea)

    # ── 标签 ──
    fwd_ret = shift_fwd(c, fwd) / c - 1

    # ── 横截面 IC ──
    print(f"\n=== Tushare 因子库反推验证 (样本{len(codes)}股, 前向{fwd}日) ===")
    print(f"{'因子':<22}{'mean IC':<10}{'IC std':<10}{'IC t值':<10}{'ICIR':<8}{'|IC|>0.03占比':<12}")
    r_rank = cs_rank(fwd_ret)
    for name, f in factors.items():
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            print(f"{name:<22} 样本不足")
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        strong = (ic.abs() > 0.03).mean() * 100
        print(f"{name:<22}{mean_ic:<10.4f}{std_ic:<10.4f}{t:<10.2f}{icir:<8.3f}{strong:<12.1f}")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(sample, fwd)
