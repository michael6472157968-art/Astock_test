"""分钟级微观结构因子挖掘 — 第二批：缩量 / 日内形态 维度。

第一批（放量维度）已证伪：尾盘/早盘放量、日内量比峰值都是日线量比的影子（2×2 分解独立增量≈0）。
本批换维度，测「放量见顶」的镜像假设：

- 日内收盘位置：(close - day_low) / (day_high - day_low)，高=强收盘/V型收复
- 量价背离度：close 近20日分位 - vol 近20日分位，高=价高量低（缩量新高背离）
- 缩量度：-日线量比，高=全天缩量
- 尾盘缩量度：-尾盘amount占比，高=尾盘缩量

（保留第一批 4 因子作对照。）

标签/基准与既有 *_test.py 一致：ATR 标准化 0.5σ、无前视、随机基准。
分钟数据 stk_mins 拉到本地缓存 backend/data/min_cache/{ts_code}.pkl。

用法: cd backend && PYTHONIOENCODING=utf-8 python minute_factor_test.py [sample_size] [forward_days] [history_days]
"""
from __future__ import annotations

import asyncio
import math
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct
from app.services.tushare_client import get_pro

THRESHOLD = 0.5
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "min_cache")


def _d8_to_dash(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def _load_minute(pro, ts_code: str, daily: list[dict], history_days: int) -> dict | None:
    if len(daily) < history_days:
        return None
    start_d8 = daily[-history_days]["trade_date"]
    end_d8 = daily[-1]["trade_date"]
    cache_file = os.path.join(CACHE_DIR, f"{ts_code}.pkl")

    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            cached = pickle.load(f)
        if cached.get("_start") == start_d8 and cached.get("_end") == end_d8:
            return cached.get("data")

    df = pro.stk_mins(
        ts_code=ts_code, freq="5min",
        start_date=_d8_to_dash(start_d8) + " 09:30:00",
        end_date=_d8_to_dash(end_d8) + " 15:00:00",
    )
    if df is None or getattr(df, "empty", True):
        return None

    data: dict[str, list] = {}
    for _, row in df.iterrows():
        tt = str(row.get("trade_time", ""))
        if len(tt) < 16:
            continue
        d8 = tt[:10].replace("-", "")
        data.setdefault(d8, []).append({
            "time": tt[11:16],
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "vol": float(row.get("vol", 0) or 0),
            "amount": float(row.get("amount", 0) or 0),
        })
    for d8 in data:
        data[d8].sort(key=lambda b: b["time"])

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, "wb") as f:
        pickle.dump({"_start": start_d8, "_end": end_d8, "data": data}, f)
    return data


def _pct_rank_value(values):
    """最后一个值在列表内的百分位(0~1)。"""
    if not values:
        return 0.5
    v = values[-1]
    return sum(1 for x in values if x <= v) / len(values)


async def run(sample_size: int, fwd: int, history_days: int):
    codes = await _sample_stocks(sample_size)
    pro = get_pro()

    # 因子名 -> [(val, vol_ratio, is_up, is_down), ...]
    collected: dict[str, list] = {
        "tail_amt_ratio": [], "open_amt_ratio": [], "tail_ret": [], "vol_peak": [],
        "day_close_pos": [], "divergence": [], "shrink": [], "tail_shrink": [],
    }
    base_up = [0, 0]
    base_down = [0, 0]
    usable = 0

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < history_days + fwd + 20:
            continue
        minute = _load_minute(pro, code, daily, history_days)
        if not minute:
            continue
        usable += 1

        for t in range(len(daily) - history_days, len(daily) - fwd):
            close = float(daily[t].get("close", 0) or 0)
            if close <= 0:
                continue
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            future_ret = (_safe_close(daily, t + fwd) - close) / close
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD

            base_up[1] += 1
            base_down[1] += 1
            if is_up:
                base_up[0] += 1
            if is_down:
                base_down[0] += 1

            td = daily[t]["trade_date"]
            bars = minute.get(td)
            if not bars:
                continue
            total_amount = sum(b["amount"] for b in bars)
            vols = [b["vol"] for b in bars]
            if total_amount <= 0 or sum(vols) <= 0 or len(bars) < 20:
                continue

            # ── 第一批因子（放量维度，对照）──
            tail_bars = [b for b in bars if b["time"] >= "14:30"]
            tail_amt = sum(b["amount"] for b in tail_bars)
            open_amt = sum(b["amount"] for b in bars if b["time"] <= "10:00")
            vols_sorted = sorted(vols)
            median_vol = vols_sorted[len(vols_sorted) // 2]
            vol_peak = (max(vols) / median_vol) if median_vol > 0 else 1.0
            tail_open = tail_bars[0]["open"] if tail_bars else close
            tail_ret = (close - tail_open) / tail_open if tail_open > 0 else 0.0

            # ── 第二批因子（缩量 / 形态维度）──
            day_low = min(b["low"] for b in bars)
            day_high = max(b["high"] for b in bars)
            day_close_pos = (close - day_low) / (day_high - day_low) if day_high > day_low else 0.5

            closes20 = [float(daily[i]["close"] or 0) for i in range(max(0, t - 19), t + 1)]
            vols20 = [float(daily[i]["volume"] or 0) for i in range(max(0, t - 19), t + 1)]
            close_rank = _pct_rank_value(closes20)
            vol_rank = _pct_rank_value(vols20)
            divergence = close_rank - vol_rank  # 高=价高量低(背离)

            avg_vol = sum(vols20) / len(vols20) if vols20 else 0.0
            vol_ratio = (float(daily[t]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0
            shrink = -vol_ratio  # 高=缩量
            tail_shrink = -(tail_amt / total_amount)  # 高=尾盘缩量

            factors = {
                "tail_amt_ratio": tail_amt / total_amount,
                "open_amt_ratio": open_amt / total_amount,
                "tail_ret": tail_ret,
                "vol_peak": vol_peak,
                "day_close_pos": day_close_pos,
                "divergence": divergence,
                "shrink": shrink,
                "tail_shrink": tail_shrink,
            }
            for name, val in factors.items():
                collected[name].append((val, vol_ratio, is_up, is_down))

    def pct(hit, total):
        return round(100.0 * hit / total, 1) if total > 0 else 0.0

    print(f"\n=== 分钟级因子验证 (样本{usable}股, 前向{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 涨 {pct(base_up[0], base_up[1])}%  跌 {pct(base_down[0], base_down[1])}%   (总{base_up[1]}样本)")
    base_up_r = pct(base_up[0], base_up[1])
    base_down_r = pct(base_down[0], base_down[1])

    labels = {
        "tail_amt_ratio": "尾盘放量占比",
        "open_amt_ratio": "早盘放量占比",
        "tail_ret": "尾盘拉升",
        "vol_peak": "日内量比峰值",
        "day_close_pos": "日内收盘位置(高=强收盘)",
        "divergence": "量价背离度(高=价高量低)",
        "shrink": "缩量度(高=缩量)",
        "tail_shrink": "尾盘缩量度(高=缩量)",
    }

    print(f"\n{'因子':<28}{'方向':<10}{'触发':<8}{'看跌':<8}{'vs基准':<10}{'看涨':<8}{'vs基准':<10}")
    for name, samples in collected.items():
        if len(samples) < 30:
            continue
        vals = sorted(s[0] for s in samples)
        hi = vals[int(0.9 * len(vals))]
        lo = vals[int(0.1 * len(vals))]

        hi_grp = [s for s in samples if s[0] >= hi]
        hi_down = pct(sum(1 for s in hi_grp if s[3]), len(hi_grp))
        hi_up = pct(sum(1 for s in hi_grp if s[2]), len(hi_grp))

        lo_grp = [s for s in samples if s[0] <= lo]
        lo_down = pct(sum(1 for s in lo_grp if s[3]), len(lo_grp))
        lo_up = pct(sum(1 for s in lo_grp if s[2]), len(lo_grp))

        print(f"{labels[name]:<28}{'高(top10%)':<10}{len(hi_grp):<8}{hi_down:<8}"
              f"{'+' if hi_down - base_down_r >= 0 else ''}{round(hi_down - base_down_r, 1)}pp{'':<4}"
              f"{hi_up:<8}{'+' if hi_up - base_up_r >= 0 else ''}{round(hi_up - base_up_r, 1)}pp")
        print(f"{'':<28}{'低(bot10%)':<10}{len(lo_grp):<8}{lo_down:<8}"
              f"{'+' if lo_down - base_down_r >= 0 else ''}{round(lo_down - base_down_r, 1)}pp{'':<4}"
              f"{lo_up:<8}{'+' if lo_up - base_up_r >= 0 else ''}{round(lo_up - base_up_r, 1)}pp")

    # ── 2×2 分解：量价背离度 × 日线量比 ──
    div_samples = collected.get("divergence", [])
    if len(div_samples) >= 100:
        vals = sorted(s[0] for s in div_samples)
        hi = vals[int(0.9 * len(vals))]
        print(f"\n── 2×2 分解: 量价背离度(阈值{hi:.2f}) × 日线量比(>2) —— 看涨/看跌 vs 基准(涨{base_up_r}%/跌{base_down_r}%) ──")
        cells = [
            ("放量>2 且 背离高(价高量低)", lambda s: s[1] > 2.0 and s[0] >= hi),
            ("放量>2 且 背离低", lambda s: s[1] > 2.0 and s[0] < hi),
            ("缩量≤2 且 背离高(价高量低)", lambda s: s[1] <= 2.0 and s[0] >= hi),
            ("缩量≤2 且 背离低", lambda s: s[1] <= 2.0 and s[0] < hi),
        ]
        for label, cond in cells:
            grp = [s for s in div_samples if cond(s)]
            if not grp:
                print(f"{label:<28} n=0")
                continue
            up = pct(sum(1 for s in grp if s[2]), len(grp))
            down = pct(sum(1 for s in grp if s[3]), len(grp))
            print(f"{label:<28} n={len(grp):<5} 看涨 {up}% ({'+' if up - base_up_r >= 0 else ''}{round(up - base_up_r, 1)}pp)  "
                  f"看跌 {down}% ({'+' if down - base_down_r >= 0 else ''}{round(down - base_down_r, 1)}pp)")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    history = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    asyncio.run(run(sample, fwd, history))
