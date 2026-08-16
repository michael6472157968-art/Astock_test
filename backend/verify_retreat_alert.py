"""验证 volume_retreat_alert 升级后 strong 级别在真实数据上正确触发。

用 DB 真实日线 + 换手率，对每只股票扫最近 30 个交易日，逐日跑升级后的
volume_retreat_alert，检查：
1. level 分布（strong 应稀有，只在量比>2 且涨 且换手率突增时触发）
2. 交叉校验：每个 level 与其返回的 vol_ratio/pct_chg/turnover_surge 是否一致
3. 打印 strong 实例的具体数值

用法: python verify_retreat_alert.py [sample_size]
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, 'backend')

from sqlalchemy import text

from app.core.database import async_session
from app.services.calibration import _sample_stocks, _load_daily_data_fast
from app.services.multi_eye import volume_retreat_alert


async def _load_turnover(ts_code: str) -> dict[str, float]:
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT trade_date, turnover_rate FROM daily_basic WHERE ts_code=:c ORDER BY trade_date ASC"),
            {"c": ts_code},
        )
        rows = r.mappings().all()
    return {str(x["trade_date"]): float(x["turnover_rate"] or 0) for x in rows}


def _check(alert: dict) -> bool:
    """校验返回 level 与 vol_ratio/pct_chg/turnover_surge 是否自洽。"""
    lv = alert["level"]
    vr = alert["vol_ratio"]
    pct = alert["pct_chg"]
    ts = alert["turnover_surge"]
    if lv == "strong":
        return vr >= 2.0 and pct > 0 and ts
    if lv == "high":
        return vr >= 2.0 and pct > 0 and not ts
    if lv == "low":
        return vr >= 2.0 and pct <= 0
    if lv == "none":
        return vr < 2.0
    return False


async def run(sample_size: int):
    codes = await _sample_stocks(sample_size)

    level_count = {"none": 0, "low": 0, "high": 0, "strong": 0}
    examples = {"strong": [], "high": [], "low": []}
    total = 0
    mismatch = 0

    for code in codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < 80:
            continue
        tmap = await _load_turnover(code)
        turnover = [tmap.get(d["trade_date"], 0.0) for d in daily]

        start = max(60, len(daily) - 30)  # 每只股票扫最近 30 天，需 >=60 天历史
        for t in range(start, len(daily)):
            alert = volume_retreat_alert(daily[:t + 1], turnover[:t + 1])
            level_count[alert["level"]] += 1
            total += 1

            if not _check(alert):
                mismatch += 1
                print(f"  [MISMATCH] {code} {daily[t]['trade_date']}: {alert}")

            lv = alert["level"]
            if lv in examples and len(examples[lv]) < 3:
                examples[lv].append((code, daily[t]["trade_date"], alert))

    print(f"\n=== volume_retreat_alert 真实数据触发验证 (样本{len(codes)}股 × 近30日, 共{total}次) ===")
    print(f"level 分布: " + "  ".join(f"{k}={v}" for k, v in level_count.items()))
    print(f"交叉校验: {'全部一致 ✅' if mismatch == 0 else f'{mismatch} 处不一致 ❌'}")

    for lv in ["strong", "high", "low"]:
        print(f"\n── {lv} 实例 ──")
        for code, td, a in examples[lv]:
            print(f"  {code} @ {td}: 量比={a['vol_ratio']}, 涨跌={a['pct_chg']}%, "
                  f"换手率突增={a['turnover_surge']}, triggered={a['triggered']}")
            print(f"      msg: {a['message']}")


if __name__ == "__main__":
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    asyncio.run(run(sample))
