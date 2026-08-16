"""板块动量 alpha 验证——横截面分位版，零成本（stocks.industry 自聚合）。

假说: A股行业轮动存在。个股所属行业过去 N 日动量在全行业的横截面分位，
能否预测个股未来方向（动量延续 vs 反转）？

- 行业日线: 聚合同 industry 个股当日平均 pct_chg
- 行业动量: 过去 N 日几何累计收益，每个交易日对全行业做横截面排名
- 因子: 行业动量排前20%(强势) / 后20%(弱势)
- 标签: ATR 标准化 0.5σ（与其他实验一致），无前视偏差
- 输出: 胜率 + 期望值(σ)

用法: python sector_momentum_test.py [sample_size] [forward_days] [momentum_days]
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


async def _load_industry_map() -> dict[str, str]:
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT ts_code, industry FROM stocks WHERE industry != ''")
        )
        rows = r.fetchall()
    return {row[0]: row[1] for row in rows}


async def _load_industry_daily() -> dict[tuple[str, str], float]:
    """(industry, trade_date) → 行业等权当日平均 pct_chg。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT s.industry, d.trade_date, d.pct_chg "
                "FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code "
                "WHERE s.industry != '' "
                "ORDER BY d.trade_date ASC"
            )
        )
        rows = r.fetchall()
    acc: dict[tuple[str, str], list[float]] = {}
    for ind, td, pct in rows:
        acc.setdefault((ind, str(td)), []).append(float(pct or 0))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def _build_momentum_rank(ind_daily: dict, trade_dates: list[str], mom_n: int) -> dict[tuple[str, str], str]:
    """(industry, trade_date) → 'strong'/'weak'/'mid'，按横截面分位。

    对每个交易日，算全行业过去 mom_n 日几何累计动量，前20%标 strong，后20%标 weak。
    """
    industries = sorted({ind for (ind, _) in ind_daily})
    # 每行业逐日的几何累计动量（只算有完整 mom_n 历史的日期）
    mom_by_ind: dict[str, dict[str, float]] = {ind: {} for ind in industries}

    for ind in industries:
        cum = 1.0
        hist: list[float] = []  # 最近 mom_n 日的几何因子
        for td in trade_dates:
            pct = ind_daily.get((ind, td))
            if pct is None:
                continue
            factor = 1.0 + pct / 100.0
            hist.append(factor)
            if len(hist) > mom_n:
                hist.pop(0)
            if len(hist) == mom_n:
                cum = 1.0
                for f in hist:
                    cum *= f
                mom_by_ind[ind][td] = cum - 1.0

    # 逐日横截面分位
    rank_map: dict[tuple[str, str], str] = {}
    for td in trade_dates:
        moms = {ind: mom_by_ind[ind].get(td) for ind in industries}
        valid = [(ind, m) for ind, m in moms.items() if m is not None]
        if len(valid) < 10:
            continue
        valid.sort(key=lambda x: x[1])
        n = len(valid)
        strong_cut = valid[int(0.8 * n)][1]   # 前20%
        weak_cut = valid[int(0.2 * n)][1]     # 后20%
        for ind, m in valid:
            if m >= strong_cut:
                rank_map[(ind, td)] = "strong"
            elif m <= weak_cut:
                rank_map[(ind, td)] = "weak"
            else:
                rank_map[(ind, td)] = "mid"
    return rank_map


async def run(sample_size: int, fwd: int, mom_n: int):
    codes = await _sample_stocks(sample_size)
    ind_map = await _load_industry_map()
    ind_daily = await _load_industry_daily()
    trade_dates = sorted({td for (_, td) in ind_daily})
    rank_map = await asyncio.to_thread(_build_momentum_rank, ind_daily, trade_dates, mom_n)

    strong_up = [0, 0, 0.0]
    strong_down = [0, 0, 0.0]
    weak_up = [0, 0, 0.0]
    weak_down = [0, 0, 0.0]
    base_up = [0, 0, 0.0]
    base_down = [0, 0, 0.0]

    for code in codes:
        industry = ind_map.get(code)
        if not industry:
            continue
        daily = await _load_daily_data_fast(code)
        if len(daily) < fwd + mom_n + 60:
            continue

        for t in range(len(daily) - fwd):
            window = daily[:t + 1]
            if len(window) < mom_n + 30:
                continue
            close = float(window[-1]["close"] or 0)
            if close <= 0:
                continue
            td = window[-1]["trade_date"]

            future_ret = (_safe_close(daily, t + fwd) - close) / close
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
            is_up = ret_norm > THRESHOLD
            is_down = ret_norm < -THRESHOLD

            base_up[0] += 1
            base_down[0] += 1
            base_up[2] += ret_norm
            base_down[2] += -ret_norm
            if is_up:
                base_up[1] += 1
            if is_down:
                base_down[1] += 1

            rank = rank_map.get((industry, td))
            if rank == "strong":
                strong_up[0] += 1
                strong_down[0] += 1
                strong_up[2] += ret_norm
                strong_down[2] += -ret_norm
                if is_up:
                    strong_up[1] += 1
                if is_down:
                    strong_down[1] += 1
            elif rank == "weak":
                weak_up[0] += 1
                weak_down[0] += 1
                weak_up[2] += ret_norm
                weak_down[2] += -ret_norm
                if is_up:
                    weak_up[1] += 1
                if is_down:
                    weak_down[1] += 1

    def hit_rate(b):
        return round(100.0 * b[1] / b[0], 1) if b[0] else 0.0

    def exp(b):
        return round(b[2] / b[0], 3) if b[0] else 0.0

    print(f"\n=== 板块动量 alpha (样本{len(codes)}股, 前向{fwd}日, 动量{mom_n}日, 横截面前/后20%, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 看涨胜率 {hit_rate(base_up)}% 期望 {exp(base_up):+.3f}σ | 看跌胜率 {hit_rate(base_down)}% 期望 {exp(base_down):+.3f}σ")
    print()
    print(f"{'因子':<24}{'触发':>9}{'方向胜率':>10}{'期望值':>9}{'vs基准胜率':>11}{'vs基准期望':>11}")
    rows = [
        ("强势行业 看涨(动量延续)", strong_up, base_up),
        ("强势行业 看跌(反转)", strong_down, base_down),
        ("弱势行业 看涨(反转)", weak_up, base_up),
        ("弱势行业 看跌(延续)", weak_down, base_down),
    ]
    for name, b, base in rows:
        hr, bhr = hit_rate(b), hit_rate(base)
        e, be = exp(b), exp(base)
        dhr = hr - bhr
        de = e - be
        print(f"{name:<24}{b[0]:>9}{hr:>9.1f}%{e:>+9.3f}σ{dhr:>+10.1f}pp{de:>+10.3f}σ")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    mom_n = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    asyncio.run(run(sample, fwd, mom_n))
