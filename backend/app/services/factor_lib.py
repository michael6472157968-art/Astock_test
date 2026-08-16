"""因子计算库——所有技术指标 + 数据清洗工具的单一真相来源。

设计原则（参照 AlphaPurify / Qlib）：
- 因子计算和因子验证解耦——本模块只负责计算，不负责分析
- 输入输出标准化：所有因子接受 list[float]，返回 list[float | None]
- 零依赖：纯 Python math 实现，不引入 numpy/pandas/polars
- 数据量级：4600 股 × 250 天 ≈ 110 万行，纯 Python list 足够
"""

from __future__ import annotations

import math


# ───────────────────────────── 基础指标 ─────────────────────────────

def sma(series: list[float], n: int) -> list[float | None]:
    """简单移动平均。前 n-1 天返回 None。"""
    out: list[float | None] = [None] * len(series)
    if len(series) < n:
        return out
    window_sum = sum(series[:n])
    out[n - 1] = window_sum / n
    for i in range(n, len(series)):
        window_sum += series[i] - series[i - n]
        out[i] = window_sum / n
    return out


def ema(series: list[float], n: int) -> list[float]:
    """指数移动平均。第 0 天为首个值本身。"""
    out: list[float] = [float(series[0])] if series else []
    k = 2 / (n + 1)
    for v in series[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes: list[float]) -> dict:
    """MACD 指标。返回 {dif, dea, bar}，每个为 list[float | None]。
    12/26/9 标准参数。
    """
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    dif: list[float | None] = [a - b for a, b in zip(ema12, ema26)]
    dea = ema([d if d is not None else 0.0 for d in dif], 9)
    bar: list[float | None] = [
        (dif[i] - dea[i]) * 2 if dif[i] is not None else None
        for i in range(len(dif))
    ]
    return {"dif": dif, "dea": dea, "bar": bar}


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """RSI 相对强弱指标。前 period 天返回 None。"""
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        rs = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        result[i + 1] = round(rs, 2)
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return result


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict:
    """布林带。返回 {mid, upper, lower}，mid 前 n-1 天为 None。"""
    mid = sma(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        std = _stddev(window, m)
        upper[i] = round(m + k * std, 2)
        lower[i] = round(m - k * std, 2)
    return {"mid": mid, "upper": upper, "lower": lower}


def kdj(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> dict:
    """KDJ 指标。返回 {k, d, j}，每条长度为 len(closes) - n + 1。"""
    k_vals, d_vals, j_vals = [], [], []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1 : i + 1])
        ll = min(lows[i - n + 1 : i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        k_prev = k_vals[-1] if k_vals else 50.0
        d_prev = d_vals[-1] if d_vals else 50.0
        k = k_prev * 2 / 3 + rsv / 3
        d = d_prev * 2 / 3 + k / 3
        j = 3 * k - 2 * d
        k_vals.append(round(k, 2))
        d_vals.append(round(d, 2))
        j_vals.append(round(j, 2))
    return {"k": k_vals, "d": d_vals, "j": j_vals}


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[float | None]:
    """平均真实波幅 ATR。前 n 天返回 None，长度与输入等长。

    手写滚动均值，保证 atr[i] 对应截至 closes[i] 的 ATR（无错位/未来函数）。
    """
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    # tr[i] = 截至第 i 日的真实波幅（i>=1），首日置 0 占位
    trs: list[float] = [0.0] * len(closes)
    for i in range(1, len(closes)):
        a = highs[i] - lows[i]
        b = abs(highs[i] - closes[i - 1])
        c = abs(lows[i] - closes[i - 1])
        trs[i] = max(a, b, c)
    # 滚动窗口 [i-n+1, i] 的 TR 均值，从 i=n 起（前 n 天无值）
    window_sum = sum(trs[1:n + 1])
    out[n] = window_sum / n
    for i in range(n + 1, len(closes)):
        window_sum += trs[i] - trs[i - n]
        out[i] = window_sum / n
    return out


# ───────────────────────────── 量价因子 ─────────────────────────────

def momentum(closes: list[float], n: int) -> list[float | None]:
    """N 日动量（收益率）。前 n 天返回 None。"""
    out: list[float | None] = [None] * len(closes)
    for i in range(n, len(closes)):
        if closes[i - n] != 0:
            out[i] = round((closes[i] - closes[i - n]) / closes[i - n], 4)
        else:
            out[i] = None
    return out


def volume_ratio(volumes: list[float], n: int = 20) -> list[float | None]:
    """量比 = 当日成交量 / N 日均量。前 n 天返回 None。"""
    ma = sma(volumes, n)
    out: list[float | None] = [None] * len(volumes)
    for i in range(n, len(volumes)):
        if ma[i] and ma[i] != 0:
            out[i] = round(volumes[i] / ma[i], 2)
    return out


def typical_price(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """典型价格 (H+L+C)/3。"""
    return [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float | None]:
    """真实波幅序列，首日返回 None。"""
    out: list[float | None] = [None]
    for i in range(1, len(closes)):
        a = highs[i] - lows[i]
        b = abs(highs[i] - closes[i - 1])
        c = abs(lows[i] - closes[i - 1])
        out.append(max(a, b, c))
    return out


# ───────────────────────────── 数据清洗 ─────────────────────────────

def winsorize_mad(series: list[float], n: float = 5.0) -> list[float]:
    """MAD 绝对中位差去极值。将偏离中位数超过 n×MAD 的值压缩到边界。
    参照 AlphaPurify / 行业标准做法。
    """
    if len(series) < 3:
        return list(series)
    sorted_s = sorted(series)
    median = sorted_s[len(sorted_s) // 2]
    abs_deviations = [abs(v - median) for v in series]
    abs_deviations.sort()
    mad = abs_deviations[len(abs_deviations) // 2] or 1.0
    upper = median + n * mad
    lower = median - n * mad
    return [lower if v < lower else (upper if v > upper else v) for v in series]


def zscore(series: list[float]) -> list[float]:
    """Z-Score 标准化 → 均值 0 标准差 1。对常量序列返回全 0。"""
    if len(series) < 2:
        return [0.0] * len(series)
    m = sum(series) / len(series)
    var = sum((v - m) ** 2 for v in series) / (len(series) - 1)
    std = var ** 0.5
    if std == 0:
        return [0.0] * len(series)
    return [round((v - m) / std, 6) for v in series]


def minmax_norm(series: list[float]) -> list[float]:
    """Min-Max 归一化到 [0, 1]。常量序列返回全 0.5。"""
    lo, hi = min(series), max(series)
    rng = hi - lo
    if rng == 0:
        return [0.5] * len(series)
    return [round((v - lo) / rng, 6) for v in series]


def clip(series: list[float], lo: float, hi: float) -> list[float]:
    """Clip values to [lo, hi] range。⚠️ 会修改输入数组以节省内存。"""
    return [lo if v < lo else (hi if v > hi else v) for v in series]


def rank_pct(series: list[float]) -> list[float]:
    """百分位排名归一化到 [0, 1]。比 min-max 更抗极端值。"""
    n = len(series)
    if n <= 1:
        return [0.5] * n
    indexed = sorted(enumerate(series), key=lambda x: x[1])
    result = [0.0] * n
    for rank, (idx, _) in enumerate(indexed):
        result[idx] = round(rank / (n - 1), 6)
    return result


def rank_pct_external(values: list[float], universe: list[float]) -> list[float]:
    """在 universe 基准上对 values 做百分位排名。
    universe 应代表全市场横截面值（如全市场 PE），values 是候选池子集。
    返回值 ∈ [0, 1] 基于 universe 的排名位置，而非池内排名。
    """
    if len(universe) < 2:
        return rank_pct(values)
    sorted_u = sorted(universe)
    nu = len(sorted_u)
    result = []
    for v in values:
        # binary search for v's position in sorted_u
        lo, hi = 0, nu - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_u[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        rank = lo
        result.append(round(rank / (nu - 1), 6))
    return result


def filter_suspended(closes: list[float]) -> list[bool]:
    """标记疑似停牌日：收盘价连续持平（基于前复权价）。"""
    if len(closes) < 2:
        return [False] * len(closes)
    out: list[bool] = [False]
    for i in range(1, len(closes)):
        out.append(closes[i] == closes[i - 1])
    return out


def filter_limit_board(
    pct_chg: list[float],
    st_mask: list[bool] | None = None,
    up_threshold: float = 9.8,
    down_threshold: float = -9.8,
) -> list[int]:
    """标记涨跌停日。返回 0=正常, 1=涨停, -1=跌停。
    主板 ±10%，ST ±5%，688/创业板 ±20%，这里取主板默认 9.8% 作为接近阈值。
    """
    out: list[int] = []
    for i, pct in enumerate(pct_chg):
        limit = 5.0 if (st_mask and st_mask[i]) else 10.0
        if pct >= limit - 0.2:
            out.append(1)
        elif pct <= -limit + 0.2:
            out.append(-1)
        else:
            out.append(0)
    return out


# ───────────────────────────── 辅助 ─────────────────────────────

def _stddev(values: list[float], mean: float) -> float:
    """样本标准差。"""
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def forward_return(closes: list[float], n: int) -> list[float | None]:
    """前向 N 日收益，用于因子验证时的标签。最后 n 天返回 None。"""
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes) - n):
        if closes[i] != 0:
            out[i] = round((closes[i + n] - closes[i]) / closes[i], 4)
    return out


def rolling_corr(a: list[float], b: list[float], n: int) -> list[float | None]:
    """滚动相关系数。前 n-1 天返回 None。"""
    out: list[float | None] = [None] * min(len(a), len(b))
    for i in range(n - 1, len(a)):
        wa = a[i - n + 1 : i + 1]
        wb = b[i - n + 1 : i + 1]
        out[i] = round(_pearson(wa, wb), 4)
    return out


def _pearson(x: list[float], y: list[float]) -> float:
    nx, ny = len(x), len(y)
    if nx < 2 or nx != ny:
        return 0.0
    mx = sum(x) / nx
    my = sum(y) / ny
    sx = sy = sxy = 0.0
    for i in range(nx):
        dx = x[i] - mx
        dy = y[i] - my
        sx += dx * dx
        sy += dy * dy
        sxy += dx * dy
    denom = (sx * sy) ** 0.5
    return sxy / denom if denom != 0 else 0.0


# ───────────────────────────── 评分引擎 ─────────────────────────────

# SQL 列约定：col0=ts_code, col1=name, col2=close, col3=pct_chg, col4=volume,
# col5+=因子列（与 config.fields 对齐顺序）
_BASE_COLS = 5


def score_and_rank(rows, config: dict, reason_prefix: str = "",
                   limit: int = 10, universe_rows: list | None = None) -> list[dict]:
    """横截面多因子评分排序——配置驱动的通用入口。

    归一化策略：
    - zscore_fields 中的因子：winsorize_mad(5σ) → zscore → clip[-3,3] → minmax[0,1]
    - PE/PB/市值等"越低越好"的因子请在 config.invert_fields 中同时列入
    - vol_ratio 特殊处理：按 |val - 1.0| 排名（越接近1分越高）
    - 其他因子：rank_pct 横截面百分位排名
    - invert_fields 中的因子：取 (1 - score) 反向

    如果提供了 universe_rows（全市场当日横截面），rank_pct 会在全市场基准上计算，
    而非仅在 rows 内部排名。这确保 IC 验证结果可推广到实时评分场景。

    config 结构（来自 factor_weights.json）：
      {"fields": [...], "weights": [...], "zscore_fields": [...],
       "invert_fields": [...], "oversold_field": "drawdown"|null}
    """
    if not rows:
        return []

    fields: list[str] = config["fields"]
    weights: list[float] = config["weights"]
    zscore_fields: set[str] = set(config.get("zscore_fields", []))
    invert_fields: set[str] = set(config.get("invert_fields", []))
    oversold_field: str | None = config.get("oversold_field")

    # 提取候选池因子值
    field_vals_all: dict[str, list[float]] = {f: [] for f in fields}
    raw_items: list[dict] = []
    for row in rows:
        fv = {}
        for i, f in enumerate(fields):
            idx = _BASE_COLS + i
            val = float(row[idx]) if idx < len(row) and row[idx] is not None else 0.0
            fv[f] = val
            field_vals_all[f].append(val)
        raw_items.append({
            "code": row[0], "name": row[1],
            "close": float(row[2]) if row[2] is not None else None,
            "pct_chg": float(row[3]) if row[3] is not None else 0,
            "vol": float(row[4]) if len(row) > 4 and row[4] is not None else 0,
            "fv": fv,
        })

    # 提取全市场基准值（如果提供）
    universe_vals_all: dict[str, list[float]] = {}
    if universe_rows:
        for f in fields:
            uvals = []
            for row in universe_rows:
                idx = _BASE_COLS + fields.index(f)
                if idx < len(row) and row[idx] is not None:
                    uvals.append(float(row[idx]))
            universe_vals_all[f] = uvals

    # 每个因子做归一化
    norm_cache: dict[str, list[float]] = {}
    for f in fields:
        vals = field_vals_all[f]
        if f == "vol_ratio":
            if universe_vals_all and f in universe_vals_all:
                udist = [abs(v - 1.0) for v in universe_vals_all[f]]
                dist = [abs(v - 1.0) for v in vals]
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct_external(dist, udist)]
            else:
                dist = [abs(v - 1.0) for v in vals]
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct(dist)]
        elif f in zscore_fields:
            w = winsorize_mad(vals, 5.0)
            z = zscore(w)
            c = clip(z, -3.0, 3.0)
            normed = minmax_norm(c)
            if f in invert_fields:
                norm_cache[f] = [round(1.0 - v, 6) for v in normed]
            else:
                norm_cache[f] = normed
        elif f in invert_fields:
            if universe_vals_all and f in universe_vals_all:
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct_external(vals, universe_vals_all[f])]
            else:
                norm_cache[f] = [round(1.0 - r, 6) for r in rank_pct(vals)]
        else:
            if universe_vals_all and f in universe_vals_all:
                norm_cache[f] = rank_pct_external(vals, universe_vals_all[f])
            else:
                norm_cache[f] = rank_pct(vals)

    # 加权评分
    scored = []
    for idx, item in enumerate(raw_items):
        score = sum(norm_cache[f][idx] * w for f, w in zip(fields, weights))
        entry = {
            "stock_code": item["code"],
            "stock_name": item["name"],
            "close": item["close"],
            "change_pct": round(item["pct_chg"], 2),
            "score": round(score * 100, 1),
            "volume": item["vol"],
            "inclusion_reason": f"{reason_prefix} 评分{round(score*100,1)}",
            "mode": reason_prefix,
        }
        if oversold_field and oversold_field in item["fv"]:
            entry["_raw_oversold_val"] = round(item["fv"][oversold_field], 2)
        scored.append(entry)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]
