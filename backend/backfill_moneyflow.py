"""一次性回填 moneyflow 个股资金流向历史数据。

按交易日全市场批量拉取（约 5500 行/天），回填 2 年（约 480 交易日）。

用法:
    cd backend && PYTHONIOENCODING=utf-8 python backfill_moneyflow.py [days]

    days 默认 730（约 2 年日历日）。已有数据的日期自动跳过，可断点续跑。
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, 'backend')


async def main(days: int):
    # 1. 确保 moneyflow_records 表存在（复用 init_db 的 create_all）
    from app.core.database import init_db
    await init_db()
    print("DB schema ensured")

    # 2. 回填
    from app.services.data_sync import sync_moneyflow_historical
    total = await sync_moneyflow_historical(days=days)
    print(f"\nBackfill complete: {total} new records")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
    asyncio.run(main(days))
