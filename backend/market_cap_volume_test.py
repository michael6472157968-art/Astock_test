"""市值 × 量比 × 换手率 分层 alpha 验证。

回答：个股按市值分层后，量比、换手率能否对短线(5日前向)产生预测力？

- 市值分层: 小盘<100亿 / 中盘100-500亿 / 大盘>500亿 (total_mv 单位万元)
- 量比: volume[-1] / mean(volume[-20:])
- 换手率突增: 当日 turnover_rate >= 近60日 90 分位
- 标签: ATR 标准化 0.5σ（与前面实验一致），无前视偏差

用法: python market_cap_volume_test.py [sample_size] [forward_days]
"""
from __future__ import annotations

import asyncio
import math
import sys

sys.path.insert(0, 'backend')

from sqlalchemy import text

from app.core.database import async_session
from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct

THRESHOLD = 0.5

# 市值分层阈值（万元）
SMALL_CAP = 1_000_000   # < 100亿
MID_CAP = 5_000_000     # 100亿 ~ 500亿
# 大盘 = > 500亿


async def _load_daily_basic(ts_code: str) -> dict[str, dict]:
    """按 trade_date 加载 daily_basic (total_mv/circ_mv/turnover_rate)。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT trade_date, total_mv, circ_mv, turnover_rate "
                "FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = {
            "total_mv": float(row["total_mv"] or 0),
            "circ_mv": float(row["circ_mv"] or 0),
            "turnover_rate": float(row["turnover_rate"] or 0),
        }
    return out


def _cap_layer(total_mv: float) -> str:
    if total_mv <= 0:
        return "none"
    if total_mv < SMALL_CAP:
        return "small"
    if total_mv < MID_CAP:
        return "mid"
    return "large"


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _pct_rank(xs, x):
    if not xs:
        return 0.5
    return sum(1 for v in xs if v <= x) / len(xs)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 不分组：市值 / 量比 单独 alpha
    cap_factors = {
        "小盘股(<100亿)看涨": [0, 0],
        "大盘股(>500亿)看涨": [0, 0],
        "大盘股(>500亿)看跌": [0, 0],
    }
    vol_factors = {
        "量比>2(放量)看涨": [0, 0],
        "量比>2(放量)看跌": [0, 0],
        "量比<0.5(缩量)看涨": [0, 0],
        "量比<0.5(缩量)看跌": [0, 0],
    }
    # 分层：每层内 量比 / 换手率
    # key: f"{layer}:{信号}"
    layered = {
        f"{layer}:量比>2看涨": [0, 0] for layer in ["small", "mid", "large"]
    }
    layered.update({
        f"{layer}:量比>2看跌": [0, 0] for layer in ["small", "mid", "large"]
    })
    layered.update({
        f"{layer}:换手突增看涨": [0, 0] for layer in ["small", "mid", "large"]
    })
    layered.update({
        f"{layer}:换手突增看跌": [0, 0] for layer in ["small", "mid", "large"]
    })

    base = [0, 0]

    for code in codes:
        daily = await _load_daily_data_fast(code)
        basic = await _load_daily_basic(code)
        if len(daily) < fwd + 60:
            continue

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 60:
                continue

            close = float(window[-1]["close"] or 0)
            if close <= 0:
                continue
            td = window[-1]["trade_date"]

            # 标签
            future_ret = (_safe_close(daily, t + fwd) - close) / close
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))

            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD
            base[1] += 1
            if is_up:
                base[0] += 1

            # 市值 + 换手率
            b = basic.get(td)
            if b is None:
                continue
            total_mv = b["total_mv"]
            layer = _cap_layer(total_mv)

            # 量比
            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            # --- 不分组：市值 ---
            if layer == "small":
                cap_factors["小盘股(<100亿)看涨"][1] += 1
                if is_up:
                    cap_factors["小盘股(<100亿)看涨"][0] += 1
            if layer == "large":
                cap_factors["大盘股(>500亿)看涨"][1] += 1
                cap_factors["大盘股(>500亿)看跌"][1] += 1
                if is_up:
                    cap_factors["大盘股(>500亿)看涨"][0] += 1
                if is_down:
                    cap_factors["大盘股(>500亿)看跌"][0] += 1

            # --- 不分组：量比 ---
            if vol_ratio > 2.0:
                vol_factors["量比>2(放量)看涨"][1] += 1
                vol_factors["量比>2(放量)看跌"][1] += 1
                if is_up:
                    vol_factors["量比>2(放量)看涨"][0] += 1
                if is_down:
                    vol_factors["量比>2(放量)看跌"][0] += 1
            elif vol_ratio < 0.5:
                vol_factors["量比<0.5(缩量)看涨"][1] += 1
                vol_factors["量比<0.5(缩量)看跌"][1] += 1
                if is_up:
                    vol_factors["量比<0.5(缩量)看涨"][0] += 1
                if is_down:
                    vol_factors["量比<0.5(缩量)看跌"][0] += 1

            # --- 分层：量比>2 ---
            if layer != "none" and vol_ratio > 2.0:
                layered[f"{layer}:量比>2看涨"][1] += 1
                layered[f"{layer}:量比>2看跌"][1] += 1
                if is_up:
                    layered[f"{layer}:量比>2看涨"][0] += 1
                if is_down:
                    layered[f"{layer}:量比>2看跌"][0] += 1

            # --- 分层：换手率突增(>90分位) ---
            trs = [
                basic[d["trade_date"]]["turnover_rate"]
                for d in window[-60:]
                if d["trade_date"] in basic and basic[d["trade_date"]]["turnover_rate"] > 0
            ]
            if layer != "none" and len(trs) >= 30 and b["turnover_rate"] > 0:
                if b["turnover_rate"] >= sorted(trs)[int(0.9 * len(trs))]:
                    layered[f"{layer}:换手突增看涨"][1] += 1
                    layered[f"{layer}:换手突增看跌"][1] += 1
                    if is_up:
                        layered[f"{layer}:换手突增看涨"][0] += 1
                    if is_down:
                        layered[f"{layer}:换手突增看跌"][0] += 1

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    base_rate = pct(base)
    print(f"\n=== 市值×量比×换手率 分层 alpha (样本{len(codes)}股, 窗口{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 未来涨 {base_rate}% (总{base[1]}样本)")

    def _show(title, factors):
        print(f"\n{title}")
        print(f"{'因子':<22}{'触发':<10}{'命中率':<10}{'vs基准':<10}")
        for name, bb in factors.items():
            rate = pct(bb)
            diff = round(rate - base_rate, 1)
            print(f"{name:<22}{bb[1]:<10}{rate:<10}{'+' if diff >= 0 else ''}{diff}pp")

    _show("── 不分组：市值 alpha ──", cap_factors)
    _show("── 不分组：量比 alpha ──", vol_factors)

    # 分层结果按 layer 分块输出
    print("\n── 分层：量比>2 预测力 ──")
    print(f"{'层级':<8}{'看涨命中率':<12}{'看涨vs基准':<12}{'看跌命中率':<12}{'看跌vs基准':<12}{'触发次数':<10}")
    for layer, label in [("small", "小盘"), ("mid", "中盘"), ("large", "大盘")]:
        up = layered[f"{layer}:量比>2看涨"]
        down = layered[f"{layer}:量比>2看跌"]
        up_r, down_r = pct(up), pct(down)
        print(f"{label:<8}{up_r:<12}{'+' if up_r-base_rate>=0 else ''}{round(up_r-base_rate,1):<12}"
              f"{down_r:<12}{'+' if down_r-base_rate>=0 else ''}{round(down_r-base_rate,1):<12}{up[1]:<10}")

    print("\n── 分层：换手率突增(>90分位) 预测力 ──")
    print(f"{'层级':<8}{'看涨命中率':<12}{'看涨vs基准':<12}{'看跌命中率':<12}{'看跌vs基准':<12}{'触发次数':<10}")
    for layer, label in [("small", "小盘"), ("mid", "中盘"), ("large", "大盘")]:
        up = layered[f"{layer}:换手突增看涨"]
        down = layered[f"{layer}:换手突增看跌"]
        up_r, down_r = pct(up), pct(down)
        print(f"{label:<8}{up_r:<12}{'+' if up_r-base_rate>=0 else ''}{round(up_r-base_rate,1):<12}"
              f"{down_r:<12}{'+' if down_r-base_rate>=0 else ''}{round(down_r-base_rate,1):<12}{up[1]:<10}")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
