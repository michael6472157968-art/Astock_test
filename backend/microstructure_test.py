"""微观结构异象验证 — 换手率突增 / 主力资金流 的前向预测力。

背景：已验证「量比>2 放量看跌」是唯一有 edge 的信号，它属于「量价/微观结构异象」，
而非 MACD/RSI 那类指标形态。本脚本用 DB 已有的 daily_basic + moneyflow 数据，
横向铺开同一类信号：

- 量比>2（基线，复现）
- 换手率突增（turnover_rate >= 近60日 90 分位）——「天量」
- 主力净流入强度 top10%（net_mf_amount/circ_mv 近60日 top10%）
- 主力净流出强度 top10%（net_mf_amount/circ_mv 近60日 bottom10%）

标签/基准与既有 *_test.py 一致：ATR 标准化 0.5σ、无前视、随机基准。

用法: python microstructure_test.py [sample_size] [forward_days]
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

# 市值分层阈值（万元），与 market_cap_volume_test.py 一致
SMALL_CAP = 1_000_000
MID_CAP = 5_000_000


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


async def _load_moneyflow(ts_code: str) -> dict[str, float]:
    """按 trade_date 加载主力净流入额 net_mf_amount（万元）。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT trade_date, net_mf_amount FROM moneyflow_records "
                "WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = float(row["net_mf_amount"] or 0)
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _pct_rank(values):
    """最后一个值在窗口内的百分位排名 (0~1)。"""
    if not values:
        return 0.5
    v = values[-1]
    return sum(1 for x in values if x <= v) / len(values)


def _cap_layer(total_mv: float) -> str:
    if total_mv <= 0:
        return "none"
    if total_mv < SMALL_CAP:
        return "small"
    if total_mv < MID_CAP:
        return "mid"
    return "large"


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    base_up = [0, 0]
    base_down = [0, 0]
    # 每个因子: [up_hit, up_total, down_hit, down_total]
    buckets = {
        "量比>2": [0, 0, 0, 0],
        "换手率突增(90分位)": [0, 0, 0, 0],
        "主力净流入top10%": [0, 0, 0, 0],
        "主力净流出top10%": [0, 0, 0, 0],
    }
    # 分层(换手率突增): 小盘/中盘/大盘 → 看跌命中
    layer_turnover = {l: [0, 0, 0, 0] for l in ["small", "mid", "large"]}

    usable = 0
    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + 60:
            continue
        basic = await _load_daily_basic(code)
        mf = await _load_moneyflow(code)
        usable += 1

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 30:
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

            base_up[1] += 1
            base_down[1] += 1
            if is_up:
                base_up[0] += 1
            if is_down:
                base_down[0] += 1

            # 量比
            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            b = basic.get(td)
            if b is None:
                b = {"total_mv": 0, "circ_mv": 0, "turnover_rate": 0}
            mf_val = mf.get(td, 0.0)

            # 因子条件
            conds = {}
            conds["量比>2"] = vol_ratio > 2.0

            # 换手率突增：近60日90分位
            trs = [basic[d["trade_date"]]["turnover_rate"]
                   for d in window[-60:]
                   if d["trade_date"] in basic and basic[d["trade_date"]]["turnover_rate"] > 0]
            conds["换手率突增(90分位)"] = (
                len(trs) >= 30 and b["turnover_rate"] > 0
                and b["turnover_rate"] >= sorted(trs)[int(0.9 * len(trs))]
            )

            # 主力净流入强度 = net_mf / circ_mv，近60日 top/bottom 10%
            mf_ratios = []
            for d in window[-60:]:
                dd = d["trade_date"]
                if dd in basic and dd in mf and basic[dd]["circ_mv"] > 0:
                    mf_ratios.append(mf[dd] / basic[dd]["circ_mv"])
            if len(mf_ratios) >= 30:
                rank = _pct_rank(mf_ratios)
                conds["主力净流入top10%"] = rank >= 0.9
                conds["主力净流出top10%"] = rank <= 0.1
            else:
                conds["主力净流入top10%"] = False
                conds["主力净流出top10%"] = False

            for name in buckets:
                if not conds[name]:
                    continue
                buckets[name][1] += 1  # up_total
                buckets[name][3] += 1  # down_total
                if is_up:
                    buckets[name][0] += 1
                if is_down:
                    buckets[name][2] += 1

            # 换手率突增 × 市值分层（看跌）
            if conds["换手率突增(90分位)"]:
                layer = _cap_layer(b["total_mv"])
                if layer in layer_turnover:
                    layer_turnover[layer][1] += 1
                    layer_turnover[layer][3] += 1
                    if is_up:
                        layer_turnover[layer][0] += 1
                    if is_down:
                        layer_turnover[layer][2] += 1

    def pct(hit, total):
        return round(100.0 * hit / total, 1) if total > 0 else 0.0

    print(f"\n=== 微观结构异象验证 (样本{usable}股, 前向{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 涨 {pct(base_up[0], base_up[1])}%  跌 {pct(base_down[0], base_down[1])}%   (总{base_up[1]}样本)")

    print(f"\n{'因子':<22}{'触发':<10}{'看涨':<10}{'看涨vs基准':<12}{'看跌':<10}{'看跌vs基准':<12}")
    base_up_r = pct(base_up[0], base_up[1])
    base_down_r = pct(base_down[0], base_down[1])
    for name, bb in buckets.items():
        up_r = pct(bb[0], bb[1])
        down_r = pct(bb[2], bb[3])
        up_diff = round(up_r - base_up_r, 1)
        down_diff = round(down_r - base_down_r, 1)
        print(f"{name:<22}{bb[1]:<10}{up_r:<10}{'+' if up_diff >= 0 else ''}{up_diff}pp{'':<6}"
              f"{down_r:<10}{'+' if down_diff >= 0 else ''}{down_diff}pp")

    print(f"\n── 换手率突增 × 市值分层（看跌）──")
    print(f"{'层级':<8}{'触发':<10}{'看跌命中率':<12}{'vs基准':<12}")
    for layer, label in [("small", "小盘"), ("mid", "中盘"), ("large", "大盘")]:
        bb = layer_turnover[layer]
        down_r = pct(bb[2], bb[3])
        diff = round(down_r - base_down_r, 1)
        print(f"{label:<8}{bb[3]:<10}{down_r:<12}{'+' if diff >= 0 else ''}{diff}pp")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
