"""用户本地数据存储——JSON+SQLite双写持久化。

每个用户一个JSON文件存放自选股，同时写入SQLite user_favorites表。
读取优先SQLite，SQLite为空时降级到JSON并自动回填。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from sqlalchemy import select, text

from app.core.database import async_session
from app.core.settings import get_settings
from app.models.orm.models import UserFavorite

logger = logging.getLogger("user_data")
_settings = get_settings()

FAVORITE_QUOTA: dict[int, int] = _settings.favorite_quota  # {0:0, 1:10, 2:20, 3:30, 99:999}


def get_quota(tier: int) -> int:
    return FAVORITE_QUOTA.get(tier, 0)


def _ensure_user_dir(user_id: int) -> str:
    path = os.path.join(_settings.user_data_dir, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _read_json(user_id: int, filename: str) -> dict:
    filepath = os.path.join(_ensure_user_dir(user_id), filename)
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _write_json(user_id: int, filename: str, data: dict) -> None:
    filepath = os.path.join(_ensure_user_dir(user_id), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.debug(f"User {user_id}: {filename} updated")


# ── 自选股操作 ──

async def get_favorites(user_id: int, offset: int = 0, limit: int = 0) -> list[dict]:
    """读取用户自选股列表，优先SQLite，降级JSON并自动回填。
    按 sort_order, created_at 排序。"""
    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(UserFavorite.user_id == user_id)
            .order_by(UserFavorite.sort_order, UserFavorite.created_at)
        )
        rows = r.scalars().all()
        if rows:
            items = [
                {
                    "stock_code": row.ts_code,
                    "stock_name": row.stock_name or "",
                    "added_at": row.created_at.isoformat() if row.created_at else "",
                    "sort_order": row.sort_order or 0,
                    "group_id": row.group_id,
                }
                for row in rows
            ]
            if offset > 0 or limit > 0:
                items = items[offset:(offset + limit) if limit else None]
            return items

    # SQLite为空，从JSON降级读取
    data = _read_json(user_id, "favorites.json")
    stocks = data.get("stocks", [])
    if stocks:
        await _backfill_sqlite(user_id, stocks)

    if offset > 0 or limit > 0:
        return stocks[offset:(offset + limit) if limit else None]
    return stocks


async def count_favorites(user_id: int) -> int:
    """统计用户自选股数量。"""
    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(UserFavorite.user_id == user_id)
        )
        return len(r.scalars().all())


async def add_favorite(user_id: int, stock_code: str, stock_name: str = "", tier: int = 0) -> tuple[bool, str]:
    """添加自选股，JSON+SQLite双写。返回(success, message)。"""
    quota = get_quota(tier)
    if quota <= 0:
        return False, "当前用户等级不支持自选功能，请升级会员"

    current = await count_favorites(user_id)
    if current >= quota:
        return False, f"自选额度已满（{current}/{quota}），请升级会员或删除部分自选"

    now = datetime.now()
    stocks_data = _read_json(user_id, "favorites.json")
    stocks = stocks_data.get("stocks", [])

    existing = [s for s in stocks if s.get("stock_code") == stock_code]
    if existing:
        return False, "已在自选列表中"

    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(
                UserFavorite.user_id == user_id, UserFavorite.ts_code == stock_code
            )
        )
        if r.scalar_one_or_none():
            return False, "已在自选列表中"

        next_order = current  # 0-based: max current index
        fav = UserFavorite(
            user_id=user_id, ts_code=stock_code, stock_name=stock_name,
            sort_order=next_order, created_at=now,
        )
        session.add(fav)

        try:
            stocks.append({
                "stock_code": stock_code,
                "stock_name": stock_name,
                "added_at": now.isoformat(),
                "sort_order": next_order,
            })
            stocks_data["stocks"] = stocks
            _write_json(user_id, "favorites.json", stocks_data)
        except Exception:
            await session.rollback()
            return False, "添加失败"

        await session.commit()
        return True, "已添加"


async def remove_favorite(user_id: int, stock_code: str) -> bool:
    """删除自选股，JSON+SQLite同步删除，并整理sort_order防止空洞。"""
    data = _read_json(user_id, "favorites.json")
    stocks = data.get("stocks", [])
    new_stocks = [s for s in stocks if s.get("stock_code") != stock_code]

    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(
                UserFavorite.user_id == user_id, UserFavorite.ts_code == stock_code
            )
        )
        fav = r.scalar_one_or_none()

        if len(new_stocks) == len(stocks) and not fav:
            return False

        if fav:
            await session.delete(fav)
            # 重新编号 sort_order 防止空洞
            remaining = await session.execute(
                select(UserFavorite).where(UserFavorite.user_id == user_id)
                .order_by(UserFavorite.sort_order, UserFavorite.created_at)
            )
            for i, row in enumerate(remaining.scalars().all()):
                row.sort_order = i

        data["stocks"] = new_stocks
        try:
            _write_json(user_id, "favorites.json", data)
        except Exception:
            await session.rollback()
            return False

        await session.commit()
        return True


async def reorder_favorites(user_id: int, ordered_codes: list[str]) -> bool:
    """按传入顺序更新 sort_order，拖拽排序后调用。"""
    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(UserFavorite.user_id == user_id)
        )
        rows = {row.ts_code: row for row in r.scalars().all()}

        for i, code in enumerate(ordered_codes):
            if code in rows:
                rows[code].sort_order = i

        await session.commit()

    # 同步更新 JSON
    data = _read_json(user_id, "favorites.json")
    stocks = data.get("stocks", [])
    for s in stocks:
        code = s.get("stock_code", "")
        if code in {code: i for i, code in enumerate(ordered_codes)}:
            s["sort_order"] = {code: i for i, code in enumerate(ordered_codes)}[code]
    _write_json(user_id, "favorites.json", data)

    return True


# ── 分组操作 ──

async def get_groups(user_id: int) -> list[dict]:
    """获取用户所有分组，含每组股票数量。"""
    async with async_session() as session:
        from sqlalchemy import func
        from app.models.orm.models import UserFavoriteGroup

        r = await session.execute(
            select(UserFavoriteGroup).where(UserFavoriteGroup.user_id == user_id)
            .order_by(UserFavoriteGroup.sort_order, UserFavoriteGroup.id)
        )
        groups = r.scalars().all()

        result = []
        for g in groups:
            cnt_r = await session.execute(
                select(func.count(UserFavorite.id)).where(
                    UserFavorite.user_id == user_id, UserFavorite.group_id == g.id
                )
            )
            result.append({
                "id": g.id, "name": g.name, "sort_order": g.sort_order,
                "stock_count": cnt_r.scalar() or 0,
            })

        # 未分组数量
        ungrouped_r = await session.execute(
            select(func.count(UserFavorite.id)).where(
                UserFavorite.user_id == user_id,
                UserFavorite.group_id.is_(None),
            )
        )
        return result, (ungrouped_r.scalar() or 0)


async def create_group(user_id: int, name: str) -> tuple[bool, str, int | None]:
    """创建分组。返回(success, message, group_id)。"""
    from app.models.orm.models import UserFavoriteGroup

    async with async_session() as session:
        exist_r = await session.execute(
            select(UserFavoriteGroup).where(
                UserFavoriteGroup.user_id == user_id,
                UserFavoriteGroup.name == name,
            )
        )
        if exist_r.scalar_one_or_none():
            return False, "分组名已存在", None

        max_r = await session.execute(
            select(UserFavoriteGroup.sort_order).where(
                UserFavoriteGroup.user_id == user_id
            ).order_by(UserFavoriteGroup.sort_order.desc())
        )
        max_order = max_r.scalar() or -1

        g = UserFavoriteGroup(user_id=user_id, name=name, sort_order=max_order + 1)
        session.add(g)
        await session.commit()
        await session.refresh(g)
        return True, "已创建", g.id


async def rename_group(user_id: int, group_id: int, name: str) -> bool:
    """重命名分组。"""
    from app.models.orm.models import UserFavoriteGroup

    async with async_session() as session:
        r = await session.execute(
            select(UserFavoriteGroup).where(
                UserFavoriteGroup.id == group_id,
                UserFavoriteGroup.user_id == user_id,
            )
        )
        g = r.scalar_one_or_none()
        if not g:
            return False
        g.name = name
        await session.commit()
        return True


async def delete_group(user_id: int, group_id: int) -> bool:
    """删除分组，组内股票 group_id 置 NULL。"""
    from app.models.orm.models import UserFavoriteGroup

    async with async_session() as session:
        r = await session.execute(
            select(UserFavoriteGroup).where(
                UserFavoriteGroup.id == group_id,
                UserFavoriteGroup.user_id == user_id,
            )
        )
        g = r.scalar_one_or_none()
        if not g:
            return False

        # 组内股票取消分组
        favs = await session.execute(
            select(UserFavorite).where(
                UserFavorite.user_id == user_id,
                UserFavorite.group_id == group_id,
            )
        )
        for f in favs.scalars().all():
            f.group_id = None

        await session.delete(g)
        await session.commit()
        return True


async def reorder_groups(user_id: int, ordered_ids: list[int]) -> bool:
    """更新分组排序。"""
    async with async_session() as session:
        from app.models.orm.models import UserFavoriteGroup

        r = await session.execute(
            select(UserFavoriteGroup).where(UserFavoriteGroup.user_id == user_id)
        )
        rows = {g.id: g for g in r.scalars().all()}
        for i, gid in enumerate(ordered_ids):
            if gid in rows:
                rows[gid].sort_order = i
        await session.commit()
        return True


async def move_to_group(user_id: int, stock_code: str, group_id: int | None) -> bool:
    """将股票移入/移出分组。group_id=None 表示取消分组。"""
    async with async_session() as session:
        r = await session.execute(
            select(UserFavorite).where(
                UserFavorite.user_id == user_id,
                UserFavorite.ts_code == stock_code,
            )
        )
        fav = r.scalar_one_or_none()
        if not fav:
            return False
        fav.group_id = group_id
        await session.commit()
        return True



async def get_favorite_codes(user_id: int) -> list[str]:
    """获取自选股代码列表"""
    stocks = await get_favorites(user_id)
    return [s["stock_code"] for s in stocks]


async def _backfill_sqlite(user_id: int, stocks: list[dict]) -> None:
    """将JSON数据回填到SQLite。"""
    async with async_session() as session:
        count = 0
        for i, s in enumerate(stocks):
            code = s.get("stock_code", "")
            if not code:
                continue
            r = await session.execute(
                select(UserFavorite).where(
                    UserFavorite.user_id == user_id, UserFavorite.ts_code == code
                )
            )
            if r.scalar_one_or_none():
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
                sort_order=s.get("sort_order", i),
                created_at=created_at,
            ))
            count += 1
        if count:
            await session.commit()
            logger.info(f"Backfilled {count} favorites for user {user_id} from JSON to SQLite")
