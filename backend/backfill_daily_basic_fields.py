"""回填 daily_basic 的 ps_ttm/dv_ttm/pe_ttm/ps/dv_ratio 历史字段。

之前 sync_daily_basic 只存了 pe/pb/total_mv/circ_mv/turnover_rate，
漏了 ps_ttm/dv_ttm 等估值字段。本脚本逐交易日补拉（一次 API 全市场），
UPDATE 已有行的缺失字段。断点续传：ps_ttm 有值即跳过。

用法: cd backend && PYTHONIOENCODING=utf-8 python backfill_daily_basic_fields.py
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from app.core.database import async_session
from app.services.tushare_client import get_daily_basic

_FIELDS = ["pe_ttm", "ps", "ps_ttm", "dv_ratio", "dv_ttm"]


async def main():
    async with async_session() as sess:
        r = await sess.execute(text("SELECT DISTINCT trade_date FROM daily_basic ORDER BY trade_date DESC"))
        dates = [row[0] for row in r.fetchall()]
    print(f"待回填 {len(dates)} 个交易日")

    done = 0
    for i, td in enumerate(dates):
        # 该日期 ps_ttm 已回填则跳过
        async with async_session() as sess:
            r = await sess.execute(
                text("SELECT COUNT(*) FROM daily_basic WHERE trade_date=:td AND ps_ttm IS NOT NULL"),
                {"td": td},
            )
            if (r.scalar() or 0) > 100:
                continue

        try:
            rows = await get_daily_basic(td)
        except Exception as e:
            print(f"  {td} 拉取失败: {e}")
            continue
        if not rows:
            continue

        async with async_session() as sess:
            for row in rows:
                try:
                    await sess.execute(text("""
                        UPDATE daily_basic
                        SET pe_ttm=:pet, ps=:ps, ps_ttm=:pst, dv_ratio=:dvr, dv_ttm=:dvt
                        WHERE ts_code=:ts AND trade_date=:td
                    """), {
                        "ts": row.get("ts_code", ""),
                        "td": str(row.get("trade_date", td)),
                        "pet": float(row.get("pe_ttm", 0) or 0) if row.get("pe_ttm") is not None else None,
                        "ps": float(row.get("ps", 0) or 0) if row.get("ps") is not None else None,
                        "pst": float(row.get("ps_ttm", 0) or 0) if row.get("ps_ttm") is not None else None,
                        "dvr": float(row.get("dv_ratio", 0) or 0) if row.get("dv_ratio") is not None else None,
                        "dvt": float(row.get("dv_ttm", 0) or 0) if row.get("dv_ttm") is not None else None,
                    })
                except Exception:
                    continue
            await sess.commit()
        done += 1
        if done % 50 == 0:
            print(f"  已回填 {done} 个交易日")
        if done % 20 == 19:
            await asyncio.sleep(1)

    print(f"完成: 回填 {done} 个交易日")


if __name__ == "__main__":
    asyncio.run(main())
