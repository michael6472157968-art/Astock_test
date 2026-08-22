"""在 Fly 上回填因子依赖的缺失数据：stock_daily + daily_basic + margin（按交易日批量）。

背景：Fly 线上库这 3 张表残缺(stock_daily 每股仅~45天有缺口 / daily_basic 18天 / margin 仅1行)，
导致诊股页 反转F1 / 融资买入F12 等因子显示"数据不足"。同步函数都按交易日批量拉(一天一次
API 调全市场~5000只)，INSERT OR REPLACE 幂等，可安全重跑。

用法（在 Fly 上，经 stdin 管道）:
  cat backfill_factor_data.py | flyctl ssh console -a astock -C "python3 -"
"""
import os
import sys
import asyncio

os.chdir('/app')
sys.path.insert(0, '/app')

from sqlalchemy import text
from app.core.database import async_session
from app.services.data_sync import sync_daily_data, sync_daily_basic
from app.services.tushare_client import get_margin_detail


async def _sync_margin_fixed(trade_date: str) -> int:
    """个股级融资融券明细 → margin_records。用 margin_detail(个股)而非 margin(交易所汇总)。"""
    rows = await get_margin_detail(trade_date)
    if not rows:
        return 0
    async with async_session() as session:
        for row in rows:
            try:
                await session.execute(text("""
                    INSERT OR REPLACE INTO margin_records
                        (trade_date, ts_code, name, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye)
                    VALUES (:td, :ts, :nm, :rzye, :rqye, :rzmre, :rqyl, :rzche, :rqchl, :rqmcl, :rzrqye)
                """), {
                    "td": str(row.get("trade_date", trade_date)),
                    "ts": row.get("ts_code", ""),
                    "nm": row.get("name", ""),
                    "rzye": float(row.get("rzye", 0) or 0),
                    "rqye": float(row.get("rqye", 0) or 0),
                    "rzmre": float(row.get("rzmre", 0) or 0),
                    "rqyl": float(row.get("rqyl", 0) or 0),
                    "rzche": float(row.get("rzche", 0) or 0),
                    "rqchl": float(row.get("rqchl", 0) or 0),
                    "rqmcl": float(row.get("rqmcl", 0) or 0),
                    "rzrqye": float(row.get("rzrqye", 0) or 0),
                })
            except Exception:
                continue
        await session.commit()
    return len(rows)

# 最近 ~125 个交易日（本地完整库提取，倒序，覆盖 20260212 -> 20260820，外加 Fly 已有的 20260821）
DATES = (
    "20260821,20260820,20260819,20260818,20260817,20260814,20260813,20260812,20260811,20260810,"
    "20260807,20260806,20260805,20260804,20260803,20260731,20260730,20260729,20260728,20260727,"
    "20260724,20260723,20260722,20260721,20260720,20260717,20260716,20260715,20260714,20260713,"
    "20260710,20260709,20260708,20260707,20260706,20260703,20260702,20260701,20260630,20260629,"
    "20260626,20260625,20260624,20260623,20260622,20260618,20260617,20260616,20260615,20260612,"
    "20260611,20260610,20260609,20260608,20260605,20260604,20260603,20260602,20260601,20260529,"
    "20260528,20260527,20260526,20260525,20260522,20260521,20260520,20260519,20260518,20260515,"
    "20260514,20260513,20260512,20260511,20260508,20260507,20260506,20260430,20260429,20260428,"
    "20260427,20260424,20260423,20260422,20260421,20260420,20260417,20260416,20260415,20260414,"
    "20260413,20260410,20260409,20260408,20260407,20260403,20260402,20260401,20260331,20260330,"
    "20260327,20260326,20260325,20260324,20260323,20260320,20260319,20260318,20260317,20260316,"
    "20260313,20260312,20260311,20260310,20260309,20260306,20260305,20260304,20260303,20260302,"
    "20260227,20260226,20260225,20260224,20260213,20260212"
).split(",")

# 完整性阈值：>= 该值视为该日已回填全，跳过（幂等重跑用）
THRESHOLDS = {"stock_daily": 5000, "daily_basic": 5000, "margin_records": 3000}


async def _count(table: str, td: str) -> int:
    async with async_session() as s:
        r = await s.execute(text(f'SELECT COUNT(*) FROM {table} WHERE trade_date = :td'), {"td": td})
        return r.scalar() or 0


async def main():
    logf = open('/app/data/backfill_factor.log', 'a', encoding='utf-8')
    def log(msg):
        print(msg, flush=True)
        logf.write(msg + '\n'); logf.flush()

    log(f"=== 回填开始 {len(DATES)} 个交易日：stock_daily + daily_basic + margin ===")
    done_d = done_b = done_m = fetch_d = fetch_b = fetch_m = 0
    for i, td in enumerate(DATES):
        line = f"[{i+1}/{len(DATES)}] {td} "
        parts = []

        cd = await _count("stock_daily", td)
        if cd >= THRESHOLDS["stock_daily"]:
            done_d += 1; parts.append(f"daily={cd}(✓)")
        else:
            try:
                n = await sync_daily_data(td); fetch_d += 1; parts.append(f"daily={cd}→+{n}")
            except Exception as e:
                parts.append(f"daily ERR {e}")

        cb = await _count("daily_basic", td)
        if cb >= THRESHOLDS["daily_basic"]:
            done_b += 1; parts.append(f"basic={cb}(✓)")
        else:
            try:
                n = await sync_daily_basic(td); fetch_b += 1; parts.append(f"basic={cb}→+{n}")
            except Exception as e:
                parts.append(f"basic ERR {e}")

        cm = await _count("margin_records", td)
        if cm >= THRESHOLDS["margin_records"]:
            done_m += 1; parts.append(f"margin={cm}(✓)")
        else:
            try:
                n = await _sync_margin_fixed(td); fetch_m += 1; parts.append(f"margin={cm}→+{n}")
            except Exception as e:
                parts.append(f"margin ERR {e}")

        log(line + "  ".join(parts))

    log(f"\n完成：daily 已全{done_d}/补{fetch_d} · basic 已全{done_b}/补{fetch_b} · margin 已全{done_m}/补{fetch_m}")
    logf.close()


asyncio.run(main())
