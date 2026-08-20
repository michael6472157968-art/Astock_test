# -*- coding: utf-8 -*-
"""补历史涨跌停数据: 把 limit_list_records 从 6 天补到与 stock_daily 同步的 ~490 天。

复用 sync_limit_list(INSERT OR REPLACE + 接口容错),逐日补齐缺失交易日。
"""
import asyncio
from app.services.data_sync import sync_limit_list
from app.core.database import async_session
from sqlalchemy import text


async def main():
    async with async_session() as sess:
        r = await sess.execute(text("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date"))
        all_dates = [row[0] for row in r.fetchall()]

    async with async_session() as sess:
        r = await sess.execute(text("SELECT DISTINCT trade_date FROM limit_list_records"))
        existing = {row[0] for row in r.fetchall()}

    missing = [d for d in all_dates if d not in existing]
    print(f'总交易日 {len(all_dates)}, 已有 {len(existing)}, 需补 {len(missing)}')

    done = 0
    empty = 0
    failed = []
    for i, td in enumerate(missing):
        try:
            n = await sync_limit_list(td)
            if n > 0:
                done += 1
            elif n == 0:
                empty += 1
        except Exception as e:
            failed.append((td, str(e)[:80]))
        if (i + 1) % 50 == 0:
            print(f'  进度 {i + 1}/{len(missing)} (成功 {done}, 空 {empty}, 失败 {len(failed)})')

    print(f'\n完成: 成功 {done}, 空 {empty}, 失败 {len(failed)}')
    if failed:
        print('失败样本:', failed[:10])


asyncio.run(main())
