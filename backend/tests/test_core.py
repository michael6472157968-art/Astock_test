"""后端核心功能测试——API响应格式、限流、缓存。"""

import asyncio
import pytest
import time


class TestAPIResponse:
    """验证统一响应格式。"""

    def test_response_format(self):
        from app.models.schemas.common import APIResponse
        resp = APIResponse(code=200, message="success", data={"key": "val"}, timestamp=123)
        d = resp.model_dump()
        assert d["code"] == 200
        assert d["message"] == "success"
        assert d["data"] == {"key": "val"}
        assert d["timestamp"] == 123
        assert d["ext_info"] == {}

    def test_pagination_format(self):
        from app.models.schemas.common import PaginatedData, PaginationParams
        p = PaginationParams(page=2, page_size=50)
        assert p.page == 2
        assert p.page_size == 50

        d = PaginatedData(total=100, page=2, page_size=50, items=[1, 2, 3])
        assert d.total == 100
        assert len(d.items) == 3


class TestCache:
    """验证内存缓存功能。"""

    @pytest.mark.asyncio
    async def test_cache_set_get(self):
        from app.core.cache import cache_set, cache_get, cache_delete
        await cache_set("test_key", {"a": 1}, ttl=60)
        val = await cache_get("test_key")
        assert val == {"a": 1}
        await cache_delete("test_key")
        assert await cache_get("test_key") is None

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self):
        from app.core.cache import cache_set, cache_get
        await cache_set("ttl_key", "value", ttl=-1)  # already expired (ttl < 0)
        val = await cache_get("ttl_key")
        assert val is None

    @pytest.mark.asyncio
    async def test_cache_stats(self):
        from app.core.cache import cache_set, cache_stats, cache_clear
        await cache_clear()
        await cache_set("perm", 1)
        await cache_set("temp", 2, ttl=3600)
        stats = await cache_stats()
        assert stats["total_keys"] == 2
        assert stats["permanent"] == 1
        assert stats["with_ttl"] == 1
        await cache_clear()
        assert (await cache_stats())["total_keys"] == 0

    @pytest.mark.asyncio
    async def test_cached_or_compute_basic(self):
        from app.core.cache import cached_or_compute, cache_clear
        await cache_clear()
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        r1 = await cached_or_compute("k1", ttl=60, compute_fn=compute)
        assert r1 == "result_1"
        assert call_count == 1

        r2 = await cached_or_compute("k1", ttl=60, compute_fn=compute)
        assert r2 == "result_1"
        assert call_count == 1  # cached

    @pytest.mark.asyncio
    async def test_cached_or_compute_mutex(self):
        from app.core.cache import cached_or_compute, cache_clear
        await cache_clear()
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return f"result_{call_count}"

        results = await asyncio.gather(*[
            cached_or_compute("k2", ttl=60, compute_fn=compute) for _ in range(5)
        ])
        assert all(r == "result_1" for r in results)
        assert call_count == 1  # only one compute ran

    @pytest.mark.asyncio
    async def test_cached_or_compute_soft_expiry(self):
        from app.core.cache import cached_or_compute, cache_clear
        await cache_clear()
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return f"v{call_count}"

        r1 = await cached_or_compute("k3", ttl=60, compute_fn=compute, soft_ttl=0)
        assert r1 == "v1"

        # soft_ttl=0 means immediately soft-expired → returns stale, bg refresh
        r2 = await cached_or_compute("k3", ttl=60, compute_fn=compute, soft_ttl=0)
        assert r2 == "v1"

        # Yield to let bg refresh start and finish
        await asyncio.sleep(0.05)
        assert call_count == 2

        r3 = await cached_or_compute("k3", ttl=60, compute_fn=compute, soft_ttl=0)
        # bg refresh already ran, got fresh value on next read
        assert call_count >= 2


class TestSecurity:
    """验证密码哈希和JWT。"""

    def test_password_hash_verify(self):
        from app.core.security import hash_password, verify_password
        hashed = hash_password("test123")
        assert verify_password("test123", hashed)
        assert not verify_password("wrong", hashed)

    def test_jwt_create_decode(self):
        from app.core.security import create_access_token, decode_token
        token = create_access_token(user_id=1, tier=2)
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["tier"] == 2
        assert payload["type"] == "access"


class TestExceptions:
    """验证异常类型。"""

    def test_stock_not_found(self):
        from app.core.exceptions import StockNotFoundError
        exc = StockNotFoundError("000001")
        assert exc.code == 404

    def test_tushare_error(self):
        from app.core.exceptions import TushareServiceError, TushareQuotaError
        assert TushareServiceError("err").code == 502
        assert TushareQuotaError("quota").code == 429
