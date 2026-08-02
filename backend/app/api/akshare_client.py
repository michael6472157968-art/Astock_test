"""AKShare 安全客户端——频率限流、本地缓存、重试降级。

AKShare 免费无额额度，但请求过快可能被封IP，因此内置 10次/分钟硬限流。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("akshare")

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "akshare"


class AKShareRateLimiter:
    """进程级滑动窗口限流器：10次/分钟硬限制。"""

    def __init__(self, max_calls: int = 10, window: float = 65.0):
        self._max = max_calls
        self._window = window
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            cutoff = now - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max:
                wait = self._timestamps[0] - cutoff + 0.5
                logger.info(f"AKShare rate limit hit, waiting {wait:.1f}s")
                await asyncio.sleep(wait)
                now = time.time()
                cutoff = now - self._window
                self._timestamps = [t for t in self._timestamps if t > cutoff]
            self._timestamps.append(time.time())


_limiter = AKShareRateLimiter(max_calls=10)


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(cache_key: str) -> Path:
    safe = cache_key.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{safe}.json"


def _read_cache(cache_key: str) -> Any | None:
    path = _cache_path(cache_key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        ttl = entry.get("_ttl", 300)
        written = entry.get("_ts", 0)
        if time.time() - written > ttl:
            return None
        return entry.get("_data")
    except Exception:
        return None


def _to_serializable(obj: Any) -> Any:
    """Convert pandas DataFrame to list-of-dicts for JSON serialization."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict(orient="records")
    return obj


def _write_cache(cache_key: str, data: Any, ttl: int) -> None:
    _ensure_cache_dir()
    path = _cache_path(cache_key)
    try:
        serializable = _to_serializable(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_ts": time.time(), "_ttl": ttl, "_data": serializable}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"AKShare cache write failed: {e}")


def is_trading_time() -> bool:
    """判断当前是否A股交易时段（周一至五 9:00-15:30）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 540 <= t <= 930


def get_cache_ttl() -> int:
    """盘中 5 分钟，盘后 24 小时。"""
    return 300 if is_trading_time() else 86400


async def safe_akshare_call(
    func_name: str,
    *args: Any,
    cache_key: str | None = None,
    retries: int = 3,
    **kwargs: Any,
) -> Any | None:
    """AKShare 安全调用：缓存→限流→重试→写缓存。"""

    if cache_key:
        cached = _read_cache(cache_key)
        if cached is not None:
            logger.debug(f"AKShare cache hit: {cache_key}")
            return cached

    await _limiter.acquire()

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            import akshare as ak
            func = getattr(ak, func_name)
            result = func(*args, **kwargs)
            if result is not None:
                if cache_key:
                    ttl = get_cache_ttl()
                    _write_cache(cache_key, result, ttl)
                return result
            return None
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = (attempt + 1) * 2
                logger.warning(f"AKShare {func_name} attempt {attempt + 1} failed: {e}, retrying in {wait}s")
                await asyncio.sleep(wait)

    logger.error(f"AKShare {func_name} failed after {retries} retries: {last_err}")
    return None
