"""迁移脚本：将现有JSON自选股数据导入SQLite user_favorites表。

遍历 data/user_data/ 下所有 favorites.json，导入 user_favorites 表。
跳过已存在的记录（唯一约束防重复），不删除原JSON文件。

用法: python -m backend.scripts.migrate_favorites
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from app.core.database import async_session, init_db
from app.core.settings import get_settings
from app.models.orm.models import UserFavorite
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_favorites")

_settings = get_settings()


async def migrate() -> None:
    await init_db()

    user_data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), _settings.user_data_dir)
    if not os.path.isdir(user_data_dir):
        logger.warning(f"User data directory not found: {user_data_dir}")
        return

    total_imported = 0
    total_skipped = 0
    total_users = 0

    for entry in os.scandir(user_data_dir):
        if not entry.is_dir():
            continue
        user_id_str = entry.name
        try:
            user_id = int(user_id_str)
        except ValueError:
            continue

        fav_file = os.path.join(entry.path, "favorites.json")
        if not os.path.exists(fav_file):
            continue

        try:
            with open(fav_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"Failed to read {fav_file}, skipping")
            continue

        stocks = data.get("stocks", [])
        if not stocks:
            continue

        total_users += 1
        imported = 0
        skipped = 0

        async with async_session() as session:
            for s in stocks:
                code = s.get("stock_code", "")
                if not code:
                    continue

                r = await session.execute(
                    select(UserFavorite).where(
                        UserFavorite.user_id == user_id, UserFavorite.ts_code == code
                    )
                )
                if r.scalar_one_or_none():
                    skipped += 1
                    continue

                added_at = s.get("added_at", "")
                try:
                    created_at = datetime.fromisoformat(added_at) if added_at else datetime.now()
                except ValueError:
                    created_at = datetime.now()

                session.add(UserFavorite(
                    user_id=user_id,
                    ts_code=code,
                    stock_name=s.get("stock_name", ""),
                    created_at=created_at,
                ))
                imported += 1

            if imported:
                await session.commit()

        if imported:
            logger.info(f"User {user_id}: imported {imported}, skipped {skipped}")
        total_imported += imported
        total_skipped += skipped

    logger.info(f"Migration complete: {total_users} users, {total_imported} imported, {total_skipped} skipped")


if __name__ == "__main__":
    asyncio.run(migrate())
