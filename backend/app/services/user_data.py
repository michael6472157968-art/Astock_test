"""用户本地数据存储——每个用户一个JSON文件，存放自选股等个人数据。

部署上线后，每个注册用户在其专属文件夹中拥有独立的 favorites.json。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from app.core.settings import get_settings

logger = logging.getLogger("user_data")
_settings = get_settings()


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

def get_favorites(user_id: int) -> list[dict]:
    """读取用户自选股列表，返回 [{stock_code, added_at}, ...]"""
    data = _read_json(user_id, "favorites.json")
    return data.get("stocks", [])


def add_favorite(user_id: int, stock_code: str, stock_name: str = "") -> bool:
    """添加自选股。已存在返回False"""
    data = _read_json(user_id, "favorites.json")
    stocks = data.get("stocks", [])
    existing = [s for s in stocks if s.get("stock_code") == stock_code]
    if existing:
        return False

    stocks.append({
        "stock_code": stock_code,
        "stock_name": stock_name,
        "added_at": datetime.now().isoformat(),
    })
    data["stocks"] = stocks
    _write_json(user_id, "favorites.json", data)
    return True


def remove_favorite(user_id: int, stock_code: str) -> bool:
    """删除自选股。不存在返回False"""
    data = _read_json(user_id, "favorites.json")
    stocks = data.get("stocks", [])
    new_stocks = [s for s in stocks if s.get("stock_code") != stock_code]
    if len(new_stocks) == len(stocks):
        return False

    data["stocks"] = new_stocks
    _write_json(user_id, "favorites.json", data)
    return True


def get_favorite_codes(user_id: int) -> list[str]:
    """获取自选股代码列表"""
    return [s["stock_code"] for s in get_favorites(user_id)]
