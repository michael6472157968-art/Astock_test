"""后端 API 全量测试——验证所有端点返回统一格式。"""

import asyncio
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app


def _make_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_health():
    async with _make_client() as c:
        resp = await c.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["data"]["db"] == "SQLite"


@pytest.mark.asyncio
async def test_register():
    async with _make_client() as c:
        phone = "138" + str(int(__import__('time').time()))[-8:]
        resp = await c.post("/api/v1/auth/register", json={
            "phone": phone, "password": "test123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data["data"]


@pytest.mark.asyncio
async def test_login():
    async with _make_client() as c:
        resp = await c.post("/api/v1/auth/login", json={
            "phone": "13800000009", "password": "test123",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()["data"]


@pytest.mark.asyncio
async def test_login_wrong():
    async with _make_client() as c:
        resp = await c.post("/api/v1/auth/login", json={
            "phone": "13800000009", "password": "wrong",
        })
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stock_pool_categories():
    async with _make_client() as c:
        resp = await c.get("/api/v1/stock-pool/categories")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["categories"]) == 4


@pytest.mark.asyncio
async def test_stock_pool_list():
    async with _make_client() as c:
        resp = await c.get("/api/v1/stock-pool/hot_leader")
        assert resp.status_code == 200
        assert "items" in resp.json()["data"]


@pytest.mark.asyncio
async def test_stock_pool_invalid():
    async with _make_client() as c:
        resp = await c.get("/api/v1/stock-pool/invalid")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_diagnosis_not_found():
    async with _make_client() as c:
        resp = await c.get("/api/v1/diagnosis/999999")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sector_ranking():
    async with _make_client() as c:
        resp = await c.get("/api/v1/sector-rotation/ranking")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_review_latest():
    async with _make_client() as c:
        resp = await c.get("/api/v1/review/latest")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_risk_list():
    async with _make_client() as c:
        resp = await c.get("/api/v1/risk-list")
        assert resp.status_code == 200
        assert "total" in resp.json()["data"]


@pytest.mark.asyncio
async def test_admin_cache_stats():
    async with _make_client() as c:
        resp = await c.get("/api/v1/admin/cache/stats")
        assert resp.status_code == 200
        assert "total_keys" in resp.json()["data"]


@pytest.mark.asyncio
async def test_alerts_all():
    async with _make_client() as c:
        for path in ["/api/v1/alerts/favorites", "/api/v1/alerts/configs", "/api/v1/alerts/notifications"]:
            resp = await c.get(path)
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_backtest():
    async with _make_client() as c:
        resp = await c.get("/api/v1/backtest/history")
        assert resp.status_code == 200
