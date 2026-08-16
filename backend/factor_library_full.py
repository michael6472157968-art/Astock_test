"""Tushare 自研因子库全量反推 — 横截面 IC 检验（10年数据）。

factor_list 接口需单独 2000元权限（无），但文档给了全部公式，用 10 年日线反推：
- Alpha101 全部 31 个（WorldQuant 横截面量价）
- 收益率多窗口（反转/动量）
- 波动率多窗口（低波动）
- 高低比多窗口（风险）
- 价格位置/MA 偏离

数据：data/long_daily.pkl（1000股 × 2016-2026）。VWAP = amount/vol。
输出：按 |IC t值| 排序的所有因子。

用法: cd backend && PYTHONIOENCODING=utf-8 python factor_library_full.py [forward_days]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

PKL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


# ── 算子（multi-index: ts_code, trade_date）──
def _roll(s, d, fn):
    return getattr(s.groupby(level="ts_code").rolling(d), fn)().reset_index(level=0, drop=True)


def ts_sum(s, d): return _roll(s, d, "sum")
def ts_mean(s, d): return _roll(s, d, "mean")
def ts_std(s, d): return _roll(s, d, "std")
def ts_max(s, d): return _roll(s, d, "max")
def ts_min(s, d): return _roll(s, d, "min")
def ts_delta(s, d): return s.groupby(level="ts_code").diff(d)
def ts_delay(s, d): return s.groupby(level="ts_code").shift(d)
def ts_ema(s, span): return s.groupby(level="ts_code").transform(lambda x: x.ewm(span=span, adjust=False).mean())


def ts_corr(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).corr(g["b"])
    ).reset_index(level=0, drop=True)


def ts_cov(a, b, d):
    ab = pd.DataFrame({"a": a, "b": b})
    return ab.groupby(level="ts_code").apply(
        lambda g: g["a"].rolling(d).cov(g["b"])
    ).reset_index(level=0, drop=True)


def cs_rank(s):
    return s.groupby(level="trade_date").rank(pct=True)


def ts_rank(s, d):
    def _r(x):
        return (x.rank().iloc[-1]) / len(x) if len(x) else 0.5
    return s.groupby(level="ts_code").rolling(d).apply(_r, raw=False).reset_index(level=0, drop=True)


def ts_argmax(s, d):
    return s.groupby(level="ts_code").rolling(d).apply(np.argmax, raw=True).reset_index(level=0, drop=True)


def shift_fwd(s, d):
    return s.groupby(level="ts_code").shift(-d)


def decay_linear(s, d):
    w = np.arange(d, 0, -1, dtype=float)
    w = w / w.sum()
    def _dl(x):
        return np.dot(x, w[:len(x)][::-1]) / w[:len(x)].sum() if len(x) else np.nan
    return s.groupby(level="ts_code").rolling(d).apply(_dl, raw=True).reset_index(level=0, drop=True)


def _w(cond, a, b):
    """np.where 的 Series 安全版（对齐 index 后保持 Series）。"""
    idx = a.index if hasattr(a, "index") else cond.index
    return pd.Series(np.where(cond, a, b), index=idx)


def main(fwd: int):
    df = pd.read_pickle(PKL)
    for col in ["open", "high", "low", "close", "vol", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ts_code", "trade_date"])
    df = df.set_index(["ts_code", "trade_date"])

    o, h, l, c, v, amt = df["open"], df["high"], df["low"], df["close"], df["vol"], df["amount"]
    ret = c.groupby(level="ts_code").pct_change()
    vwap = (amt / v.replace(0, np.nan)).fillna(c)
    adv20 = ts_mean(v, 20)

    F: dict[str, pd.Series] = {}

    # ── 收益率多窗口 ──
    for n in [5, 10, 21, 42, 63, 126, 252]:
        F[f"return_{n}d"] = c.groupby(level="ts_code").pct_change(n)

    # ── 波动率多窗口 ──
    for n in [21, 42, 63, 126, 252]:
        F[f"vol_{n}d"] = ts_std(ret, n)

    # ── 高低比多窗口 ──
    for n in [21, 42, 63, 126, 252]:
        hi = ts_max(h, n)
        lo = ts_min(l, n)
        F[f"highlow_{n}d"] = hi / lo.replace(0, np.nan)

    # ── 价格位置 ──
    F["ma_bias_20d"] = c / ts_mean(c, 20) - 1

    # ── Alpha101 31 个 ──
    def A(name, s):
        F[name] = s

    A("a101_1", cs_rank(ts_argmax(_w(ret < 0, ts_std(ret, 20), c), 5)) - 0.5)
    A("a101_2", -ts_corr(cs_rank(ts_delta(np.log(v.replace(0, np.nan)), 2)), cs_rank((c - o) / o), 6))
    A("a101_3", -ts_corr(cs_rank(o), cs_rank(v), 10))
    A("a101_4", -ts_rank(cs_rank(l), 9))
    A("a101_5", cs_rank(o - ts_mean(vwap, 10)) * (-np.abs(cs_rank(c - vwap))))
    A("a101_6", -ts_corr(o, v, 10))
    A("a101_7", _w(adv20 < v, -ts_rank(np.abs(ts_delta(c, 7)), 60) * np.sign(ts_delta(c, 7)), -1.0))
    A("a101_8", -cs_rank((ts_sum(o, 5) * ts_sum(ret, 5)) - ts_delay(ts_sum(o, 5) * ts_sum(ret, 5), 10)))
    A("a101_9", _w(0 < ts_min(ts_delta(c, 1), 5), ts_delta(c, 1),
                   _w(ts_max(ts_delta(c, 1), 5) < 0, ts_delta(c, 1), -ts_delta(c, 1))))
    A("a101_10", _w(0 < ts_min(ts_delta(c, 1), 4), ts_delta(c, 1),
                    _w(ts_max(ts_delta(c, 1), 4) < 0, ts_delta(c, 1), -ts_delta(c, 1))))
    A("a101_11", (cs_rank(ts_max(vwap - c, 3)) + cs_rank(ts_min(vwap - c, 3))) * cs_rank(ts_delta(v, 3)))
    A("a101_12", np.sign(ts_delta(v, 1)) * (-ts_delta(c, 1)))
    A("a101_13", -cs_rank(ts_cov(cs_rank(c), cs_rank(v), 5)))
    A("a101_14", -cs_rank(ts_delta(ret, 3)) * ts_corr(o, v, 10))
    A("a101_15", -ts_sum(cs_rank(ts_corr(cs_rank(h), cs_rank(v), 3)), 3))
    A("a101_16", -cs_rank(ts_cov(cs_rank(h), cs_rank(v), 5)))
    A("a101_17", -cs_rank(ts_rank(c, 10)) * cs_rank(ts_delta(ts_delta(c, 1), 1)) * cs_rank(ts_rank(v / adv20, 5)))
    A("a101_18", -cs_rank(ts_std(np.abs(c - o), 5) + (c - o) + ts_corr(c, o, 10)))
    A("a101_19", (-np.sign((c - ts_delay(c, 7)) + ts_delta(c, 7))) * (1 + cs_rank(1 + ts_sum(ret, 250))))
    A("a101_20", -cs_rank(o - ts_delay(h, 1)) * cs_rank(o - ts_delay(c, 1)) * cs_rank(o - ts_delay(l, 1)))
    A("a101_22", -ts_delta(ts_corr(h, v, 5), 5) * cs_rank(ts_std(c, 20)))
    A("a101_23", (-ts_delta(h, 2)).where(ts_sum(h, 20) / 20 < h, 0.0))
    A("a101_25", cs_rank(-ret * adv20 * vwap * (h - c)))
    A("a101_33", cs_rank(-(1 - o / c)))
    A("a101_34", cs_rank(1 - cs_rank(ts_std(ret, 2) / ts_std(ret, 5)) + 1 - cs_rank(ts_delta(c, 1))))
    A("a101_41", np.sqrt(h * l) - vwap)
    A("a101_52", (-ts_min(l, 5) + ts_delay(ts_min(l, 5), 5)) * cs_rank((ts_sum(ret, 240) - ts_sum(ret, 20)) / 220) * ts_rank(v, 5))
    A("a101_53", -ts_delta(((c - l) - (h - c)) / (c - l).replace(0, np.nan), 9))
    A("a101_54", (-(l - c) * o ** 5) / ((l - h) * c ** 5))
    A("a101_57", -(c - vwap) / decay_linear(cs_rank(ts_argmax(c, 30)), 2))
    A("a101_101", (c - o) / (h - l + 0.001))

    # ── 标签 + IC 检验 ──
    fwd_ret = shift_fwd(c, fwd) / c - 1
    r_rank = cs_rank(fwd_ret)

    print(f"\n=== Tushare 自研因子库全量反推 (1000股 × 10年, 前向{fwd}日) ===")
    results = []
    for name, f in F.items():
        f_rank = cs_rank(f)
        tmp = pd.DataFrame({"f": f_rank, "r": r_rank}).dropna()
        ic = tmp.groupby(level="trade_date").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            continue
        mean_ic = ic.mean()
        std_ic = ic.std()
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        results.append((name, mean_ic, std_ic, t, mean_ic / std_ic if std_ic > 0 else 0))

    results.sort(key=lambda x: -abs(x[3]))
    print(f"{'因子':<22}{'mean IC':<10}{'IC t值':<10}{'ICIR':<8}")
    for name, mic, sic, t, icir in results:
        mark = " ★" if abs(t) > 5 else ""
        print(f"{name:<22}{mic:<10.4f}{t:<10.2f}{icir:<8.3f}{mark}")


if __name__ == "__main__":
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(fwd)
