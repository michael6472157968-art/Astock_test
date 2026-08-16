"""独立信息源 alpha 验证——量价背离 / 放量突破 / 换手率异常 / 估值分位。

回答：价格之外的信息源（成交量、换手率、估值）能否预测方向，超过随机基准？

与 resonance_test.py 完全相同的标签体系：
- 标签: ATR 标准化 ret / (atr_pct * sqrt(fwd))，阈值 0.5σ
- 随机基准: 任意一天未来涨/跌超 0.5σ 的概率（约 22.5%）
- 无前视偏差: 因子只用 t 及之前的数据

用法: python independent_alpha_test.py [sample_size] [forward_days]
"""
from __future__ import annotations

import asyncio
import math
import sys
from collections import defaultdict

sys.path.insert(0, 'backend')

from sqlalchemy import text

from app.core.database import async_session
from app.services.calibration import _sample_stocks, _load_daily_data_fast, _atr_pct

THRESHOLD = 0.5  # 0.5 个标准差


async def _load_daily_basic(ts_code: str) -> dict[str, dict]:
    """按 trade_date 加载个股 daily_basic (turnover_rate/pe/pb)。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT trade_date, pe, pb, turnover_rate "
                "FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        out[str(row["trade_date"])] = {
            "pe": float(row["pe"] or 0),
            "pb": float(row["pb"] or 0),
            "turnover_rate": float(row["turnover_rate"] or 0),
        }
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _pct_rank(xs, x):
    """x 在 xs 中的分位 (0~1)，无前视。"""
    if not xs:
        return 0.5
    return sum(1 for v in xs if v <= x) / len(xs)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 每个因子: [hit, total]，hit = 预测方向命中 (涨>0.5σ 或 跌<-0.5σ)
    factors = {
        "量价背离(看跌)": [0, 0],      # 新高但缩量 → 预测跌
        "放量突破(看涨)": [0, 0],      # 放量+破20日高 → 预测涨
        "换手率突增(看涨)": [0, 0],    # 换手率>90分位 → 预测涨
        "换手率突增(看跌)": [0, 0],    # 同上 → 预测跌（分开测方向）
        "估值低位(看涨)": [0, 0],      # PE<20分位 → 预测涨
        "估值高位(看跌)": [0, 0],      # PE>80分位 → 预测跌
    }
    base = [0, 0]  # 随机基准: [上涨次数, 总次数]

    for code in codes:
        daily = await _load_daily_data_fast(code)
        basic = await _load_daily_basic(code)
        if len(daily) < fwd + 60:
            continue

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 60:
                continue

            td = window[-1]["trade_date"]
            close = float(window[-1]["close"] or 0)
            vol = float(window[-1]["volume"] or 0)
            if close <= 0:
                continue

            # 标签 (ATR 标准化)
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

            # --- 因子计算（只用 t 及之前数据） ---
            vols = [float(d["volume"] or 0) for d in window[-20:]]
            closes_20 = [float(d["close"] or 0) for d in window[-20:]]
            closes_21_prev = [float(d["close"] or 0) for d in window[-21:-1]]
            avg_vol = _mean(vols)

            # 1. 量价背离: 收盘创20日新高，但成交量低于20日均量 → 看跌
            if avg_vol > 0 and close >= max(closes_20, default=0) and vol < avg_vol:
                factors["量价背离(看跌)"][1] += 1
                if is_down:
                    factors["量价背离(看跌)"][0] += 1

            # 2. 放量突破: 量>2×均量 且 收盘突破前20日高点 → 看涨
            if avg_vol > 0 and vol > 2.0 * avg_vol and closes_21_prev and close > max(closes_21_prev):
                factors["放量突破(看涨)"][1] += 1
                if is_up:
                    factors["放量突破(看涨)"][0] += 1

            # 3. 换手率突增 (需 daily_basic 覆盖该日期)
            b = basic.get(td)
            if b and b["turnover_rate"] > 0:
                trs = [
                    basic[d["trade_date"]]["turnover_rate"]
                    for d in window[-60:]
                    if d["trade_date"] in basic and basic[d["trade_date"]]["turnover_rate"] > 0
                ]
                if len(trs) >= 30 and b["turnover_rate"] >= sorted(trs)[int(0.9 * len(trs))]:
                    factors["换手率突增(看涨)"][1] += 1
                    factors["换手率突增(看跌)"][1] += 1
                    if is_up:
                        factors["换手率突增(看涨)"][0] += 1
                    if is_down:
                        factors["换手率突增(看跌)"][0] += 1

            # 4. 估值分位 (PE 需 >0)
            if b and b["pe"] > 0:
                pes = [
                    basic[d["trade_date"]]["pe"]
                    for d in window
                    if d["trade_date"] in basic and basic[d["trade_date"]]["pe"] > 0
                ]
                if len(pes) >= 60:
                    rk = _pct_rank(pes, b["pe"])
                    if rk <= 0.2:
                        factors["估值低位(看涨)"][1] += 1
                        if is_up:
                            factors["估值低位(看涨)"][0] += 1
                    if rk >= 0.8:
                        factors["估值高位(看跌)"][1] += 1
                        if is_down:
                            factors["估值高位(看跌)"][0] += 1

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    print(f"\n=== 独立信息源 alpha (样本{len(codes)}股, 窗口{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 未来涨 {pct(base)}% (总{base[1]}样本)")
    print(f"\n{'因子':<20}{'触发次数':<12}{'方向命中率':<12}{'vs基准':<10}")
    base_rate = pct(base)
    for name, b in factors.items():
        rate = pct(b)
        diff = round(rate - base_rate, 1)
        print(f"{name:<20}{b[1]:<12}{rate:<12}{'+' if diff >= 0 else ''}{diff}pp")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    asyncio.run(run(sample, fwd))
