"""进程内内存缓存——并发互斥 + 软过期。

API 兼容，现有 cache_get/cache_set/cache_delete/cache_stats/cache_clear 保持不变。
新增 cached_or_compute() 提供并发互斥和可选软过期能力。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

_logger = logging.getLogger(__name__)

# (value, hard_expires_at, soft_expires_at)
_CACHE: dict[str, tuple[Any, float | None, float | None]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
_REFRESH_TASKS: dict[str, asyncio.Task] = {}


def _now() -> float:
    return time.monotonic()


def _purge_expired() -> None:
    now = _now()
    expired = [k for k, (_, exp, _) in _CACHE.items() if exp is not None and exp < now]
    for k in expired:
        del _CACHE[k]


async def cache_get(key: str) -> Any:
    _purge_expired()
    entry = _CACHE.get(key)
    return entry[0] if entry is not None else None


async def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    expires_at = _now() + ttl if ttl is not None else None
    _CACHE[key] = (value, expires_at, None)


async def cache_delete(key: str) -> None:
    _CACHE.pop(key, None)


async def cache_stats() -> dict:
    _purge_expired()
    return {
        "total_keys": len(_CACHE),
        "permanent": sum(1 for _, exp, _ in _CACHE.values() if exp is None),
        "with_ttl": sum(1 for _, exp, _ in _CACHE.values() if exp is not None),
        "active_locks": len(_LOCKS),
        "refreshing": len(_REFRESH_TASKS),
    }


async def cache_clear() -> None:
    _CACHE.clear()


async def cached_or_compute(
    key: str,
    ttl: int,
    compute_fn: Callable[[], Awaitable[Any]],
    *,
    soft_ttl: int | None = None,
) -> Any:
    """缓存优先 + 并发互斥 + 可选软过期。

    - 缓存新鲜 → 直接返回
    - 软过期命中 → 返回旧值，后台异步刷新（不阻塞当前请求）
    - 缓存未命中 → per-key 互斥锁，仅一个请求执行 compute_fn，其余等待复用结果
    """
    now = _now()

    # 快速路径：检查缓存
    entry = _CACHE.get(key)
    if entry is not None:
        hard_exp = entry[1]
        if hard_exp is None or hard_exp > now:
            soft_exp = entry[2]
            if soft_exp is not None and soft_exp <= now:
                if key not in _REFRESH_TASKS:
                    _REFRESH_TASKS[key] = asyncio.ensure_future(
                        _bg_refresh(key, ttl, soft_ttl, compute_fn)
                    )
                return entry[0]
            return entry[0]

    # 串行化同一 key 的并发请求
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        # 双重检查：可能在等待锁期间已被填充
        entry = _CACHE.get(key)
        if entry is not None:
            hard_exp = entry[1]
            if hard_exp is None or hard_exp > _now():
                return entry[0]

        result = await compute_fn()
        if result is not None:
            soft_exp = _now() + soft_ttl if soft_ttl is not None else None
            _CACHE[key] = (result, _now() + ttl, soft_exp)
        return result


async def _bg_refresh(
    key: str, ttl: int, soft_ttl: int | None, compute_fn: Callable[[], Awaitable[Any]]
) -> None:
    try:
        result = await compute_fn()
        if result is not None:
            soft_exp = _now() + soft_ttl if soft_ttl is not None else None
            _CACHE[key] = (result, _now() + ttl, soft_exp)
    except Exception:
        _logger.warning("Background cache refresh failed for key=%s", key, exc_info=True)
    finally:
        _REFRESH_TASKS.pop(key, None)
