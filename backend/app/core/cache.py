"""进程内内存缓存——带TTL过期的字典实现。

替代Redis，零外部依赖。API与原redis.py兼容。
"""

from __future__ import annotations

import time
from typing import Any

_CACHE: dict[str, tuple[Any, float | None]] = {}  # key → (value, expires_at)


def _now() -> float:
    return time.monotonic()


def _purge_expired() -> None:
    now = _now()
    expired = [k for k, (_, exp) in _CACHE.items() if exp is not None and exp < now]
    for k in expired:
        del _CACHE[k]


async def cache_get(key: str) -> Any:
    _purge_expired()
    entry = _CACHE.get(key)
    return entry[0] if entry is not None else None


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    expires_at = _now() + ttl if ttl else None
    _CACHE[key] = (value, expires_at)


async def cache_delete(key: str) -> None:
    _CACHE.pop(key, None)


async def cache_stats() -> dict:
    _purge_expired()
    return {
        "total_keys": len(_CACHE),
        "permanent": sum(1 for _, exp in _CACHE.values() if exp is None),
        "with_ttl": sum(1 for _, exp in _CACHE.values() if exp is not None),
    }


async def cache_clear() -> None:
    _CACHE.clear()
