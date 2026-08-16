"""主力资金流 alpha 验证——net_mf_amount 是否预测方向。

与 independent_alpha_test.py 相同标签体系（ATR 标准化 0.5σ），无前视偏差。
主力净流入用成交额 amount 标准化为「净流入率」，消除大盘股/小盘股不可比。

因子设计:
- 主力净流入率 = net_mf_amount(万元) / amount(千元)，单位统一后为小数
- f1 当日净流入率分位(近60日) → 高=主力吸筹看涨
- f2 5日累计净流入率 → 持续流入看涨
- f3 净流入率突增(>80分位) → 看涨 / 突降(<20分位) → 看跌

用法: python moneyflow_alpha_test.py [sample_size] [forward_days]
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


async def _load_moneyflow(ts_code: str) -> dict[str, float]:
    """按 trade_date 加载主力净流入率(net_mf_amount/amount，标准化后)。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT m.trade_date, m.net_mf_amount, d.amount "
                "FROM moneyflow_records m "
                "JOIN stock_daily d ON d.ts_code = m.ts_code AND d.trade_date = m.trade_date "
                "WHERE m.ts_code=:c ORDER BY m.trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    out = {}
    for row in rows:
        net_mf = float(row["net_mf_amount"] or 0)
        amount = float(row["amount"] or 0)
        if amount > 0:
            # net_mf(万元) * 10000 / (amount(千元) * 1000) = net_mf * 10 / amount
            out[str(row["trade_date"])] = net_mf * 10.0 / amount
    return out


def _pct_rank(xs, x):
    if not xs:
        return 0.5
    return sum(1 for v in xs if v <= x) / len(xs)


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    factors = {
        "主力净流入率高位(看涨)": [0, 0],   # 当日净流入率 > 80分位
        "主力净流入率低位(看跌)": [0, 0],   # 当日净流入率 < 20分位
        "5日累计净流入(看涨)": [0, 0],      # 5日累计净流入率 > 0
        "5日累计净流出(看跌)": [0, 0],      # 5日累计净流入率 < 0
    }
    base = [0, 0]

    for code in codes:
        daily = await _load_daily_data_fast(code)
        mf = await _load_moneyflow(code)
        if len(daily) < fwd + 60 or len(mf) < 60:
            continue

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < 60:
                continue

            close = float(window[-1]["close"] or 0)
            if close <= 0:
                continue
            td = window[-1]["trade_date"]

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

            # 当日净流入率 + 近60日序列
            cur_ratio = mf.get(td)
            if cur_ratio is None:
                continue
            ratios = [
                mf.get(d["trade_date"])
                for d in window[-60:]
                if mf.get(d["trade_date"]) is not None
            ]
            if len(ratios) < 30:
                continue

            # f1: 当日分位
            rk = _pct_rank(ratios, cur_ratio)
            if rk >= 0.8:
                factors["主力净流入率高位(看涨)"][1] += 1
                if is_up:
                    factors["主力净流入率高位(看涨)"][0] += 1
            if rk <= 0.2:
                factors["主力净流入率低位(看跌)"][1] += 1
                if is_down:
                    factors["主力净流入率低位(看跌)"][0] += 1

            # f2: 5日累计净流入率
            ratio_5 = [
                mf.get(d["trade_date"])
                for d in window[-5:]
                if mf.get(d["trade_date"]) is not None
            ]
            if len(ratio_5) >= 5:
                cum = sum(ratio_5)
                if cum > 0:
                    factors["5日累计净流入(看涨)"][1] += 1
                    if is_up:
                        factors["5日累计净流入(看涨)"][0] += 1
                else:
                    factors["5日累计净流出(看跌)"][1] += 1
                    if is_down:
                        factors["5日累计净流出(看跌)"][0] += 1

    def pct(b):
        return round(100.0 * b[0] / b[1], 1) if b[1] > 0 else 0.0

    print(f"\n=== 主力资金流 alpha (样本{len(codes)}股, 窗口{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 未来涨 {pct(base)}% (总{base[1]}样本)")
    print(f"\n{'因子':<22}{'触发次数':<12}{'方向命中率':<12}{'vs基准':<10}")
    base_rate = pct(base)
    for name, b in factors.items():
        rate = pct(b)
        diff = round(rate - base_rate, 1)
        print(f"{name:<22}{b[1]:<12}{rate:<12}{'+' if diff >= 0 else ''}{diff}pp")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    asyncio.run(run(sample, fwd))
