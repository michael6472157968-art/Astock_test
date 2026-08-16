"""期望值/赔率补测——之前只测胜率，这里测「按信号方向开仓的平均盈亏」。

核心问题：胜率高 ≠ 赚钱。若赢时只赢 0.6σ、输时输 3σ，胜率再高期望值也为负。

- 每个信号按方向取交易收益: 看跌 → trade = -ret_norm, 看涨 → trade = +ret_norm
- 期望值 = mean(trade)，单位 σ（跨窗口可比）
- 赔率 = 平均盈利σ / 平均亏损σ（大赚 +0.5σ 以上记盈，大亏 -0.5σ 以下记亏）
- 基准: 随机做多 mean(ret_norm)、随机做空 -mean(ret_norm)

用法: python expectancy_test.py [sample_size] [forward_days]
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


async def _load_daily_basic(ts_code: str) -> dict[str, dict]:
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
            out[str(row["trade_date"])] = net_mf * 10.0 / amount
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _pct_rank(xs, x):
    if not xs:
        return 0.5
    return sum(1 for v in xs if v <= x) / len(xs)


class Bucket:
    """按信号方向累计交易收益，统计胜率/期望值/赔率。"""

    __slots__ = ("n", "sum_trade", "win_n", "win_sum", "loss_n", "loss_sum")

    def __init__(self):
        self.n = 0
        self.sum_trade = 0.0
        self.win_n = 0
        self.win_sum = 0.0
        self.loss_n = 0
        self.loss_sum = 0.0

    def add(self, trade: float):
        self.n += 1
        self.sum_trade += trade
        if trade > THRESHOLD:
            self.win_n += 1
            self.win_sum += trade
        elif trade < -THRESHOLD:
            self.loss_n += 1
            self.loss_sum += -trade  # 取正数方便算赔率

    def expectancy(self) -> float:
        return self.sum_trade / self.n if self.n else 0.0

    def win_rate(self) -> float:
        return self.win_n / self.n if self.n else 0.0

    def avg_win(self) -> float:
        return self.win_sum / self.win_n if self.win_n else 0.0

    def avg_loss(self) -> float:
        return self.loss_sum / self.loss_n if self.loss_n else 0.0

    def payoff(self) -> float:
        al = self.avg_loss()
        return self.avg_win() / al if al > 0 else 0.0


async def run(sample_size: int, fwd: int):
    codes = await _sample_stocks(sample_size)

    # 信号 → 方向(+1做多/-1做空)
    signals: dict[str, tuple[Bucket, int]] = {
        "量比>2 看跌(做空)": (Bucket(), -1),
        "量比>2 看涨(做多)": (Bucket(), +1),
        "量比<0.5 看跌(做空)": (Bucket(), -1),
        "量比<0.5 看涨(做多)": (Bucket(), +1),
        "换手突增 看跌(做空)": (Bucket(), -1),
        "换手突增 看涨(做多)": (Bucket(), +1),
        "资金流高位 看涨(做多)": (Bucket(), +1),
        "资金流低位 看跌(做空)": (Bucket(), -1),
    }

    base_long = Bucket()   # 随机做多 = 每笔都取 +ret_norm
    base_short = Bucket()  # 随机做空 = 每笔都取 -ret_norm

    for code in codes:
        daily = await _load_daily_data_fast(code)
        basic = await _load_daily_basic(code)
        mf = await _load_moneyflow(code)
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

            future_ret = (_safe_close(daily, t + fwd) - close) / close
            atr_pct = _atr_pct(daily, t)
            if atr_pct <= 0:
                continue
            ret_norm = future_ret / (atr_pct * math.sqrt(fwd))

            # 基准：随机做多/做空
            base_long.add(ret_norm)
            base_short.add(-ret_norm)

            b = basic.get(td)

            # 量比
            vols = [float(d["volume"] or 0) for d in window[-20:]]
            avg_vol = _mean(vols)
            vol_ratio = (float(window[-1]["volume"] or 0) / avg_vol) if avg_vol > 0 else 1.0

            if vol_ratio > 2.0:
                signals["量比>2 看跌(做空)"][0].add(-ret_norm)
                signals["量比>2 看涨(做多)"][0].add(ret_norm)
            elif vol_ratio < 0.5:
                signals["量比<0.5 看跌(做空)"][0].add(-ret_norm)
                signals["量比<0.5 看涨(做多)"][0].add(ret_norm)

            # 换手率突增(>90分位)
            if b is not None and b["turnover_rate"] > 0:
                trs = [
                    basic[d["trade_date"]]["turnover_rate"]
                    for d in window[-60:]
                    if d["trade_date"] in basic and basic[d["trade_date"]]["turnover_rate"] > 0
                ]
                if len(trs) >= 30 and b["turnover_rate"] >= sorted(trs)[int(0.9 * len(trs))]:
                    signals["换手突增 看跌(做空)"][0].add(-ret_norm)
                    signals["换手突增 看涨(做多)"][0].add(ret_norm)

            # 主力资金流分位
            cur_ratio = mf.get(td)
            if cur_ratio is not None:
                ratios = [
                    mf.get(d["trade_date"])
                    for d in window[-60:]
                    if mf.get(d["trade_date"]) is not None
                ]
                if len(ratios) >= 30:
                    rk = _pct_rank(ratios, cur_ratio)
                    if rk >= 0.8:
                        signals["资金流高位 看涨(做多)"][0].add(ret_norm)
                    if rk <= 0.2:
                        signals["资金流低位 看跌(做空)"][0].add(-ret_norm)

    print(f"\n=== 期望值/赔率补测 (样本{len(codes)}股, 窗口{fwd}日, 阈值{THRESHOLD}σ) ===")
    print(f"随机基准: 做多期望 {base_long.expectancy():+.3f}σ  做空期望 {base_short.expectancy():+.3f}σ")
    print(f"          (基准做多胜率 {base_long.win_rate()*100:.1f}%, 赔率 {base_long.payoff():.2f})")
    print()
    print(f"{'信号':<20}{'触发':<9}{'胜率':<9}{'期望值':<11}{'平均盈':<9}{'平均亏':<9}{'赔率':<7}{'vs基准':<9}")
    for name, (bkt, direction) in signals.items():
        exp = bkt.expectancy()
        # vs基准: 看跌信号对比做空基准, 看涨信号对比做多基准
        base_exp = base_short.expectancy() if direction == -1 else base_long.expectancy()
        diff = exp - base_exp
        print(f"{name:<20}{bkt.n:<9}{bkt.win_rate()*100:<8.1f}%"
              f"{exp:+.3f}σ{'':<4}{bkt.avg_win():<8.2f}{bkt.avg_loss():<8.2f}{bkt.payoff():<6.2f}{diff:+.3f}σ")


def _safe_close(daily, idx):
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fwd = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    asyncio.run(run(sample, fwd))
