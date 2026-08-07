"""Security boundaries for position-ownership API failures."""

import inspect

import pytest
from flask import g

from app.routes import strategy_position_ownership_routes as routes


def test_ownership_read_does_not_expose_internal_exception(app, monkeypatch):
    monkeypatch.setattr(
        routes,
        "_load_ownership_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private database detail")),
    )
    with app.test_request_context("/api/strategies/position-ownership?id=1"):
        g.user_id = 1
        response, status = inspect.unwrap(routes.get_position_ownership)()

    payload = response.get_json()
    assert status == 500
    assert payload["msg"] == "positionOwnership.loadFailed"
    assert "private database detail" not in str(payload)


def test_ownership_repair_does_not_expose_internal_exception(app, monkeypatch):
    monkeypatch.setattr(
        routes,
        "_load_ownership_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private exchange detail")),
    )
    with app.test_request_context(
        "/api/strategies/position-ownership/repair",
        method="POST",
        json={"id": 1, "symbol": "BTC/USDT", "side": "long", "action": "recheck"},
    ):
        g.user_id = 1
        response, status = inspect.unwrap(routes.repair_position_ownership_route)()

    payload = response.get_json()
    assert status == 500
    assert payload["msg"] == "positionOwnership.repairFailed"
    assert "private exchange detail" not in str(payload)


def test_ownership_repair_rejects_non_numeric_strategy_id(app):
    with app.test_request_context(
        "/api/strategies/position-ownership/repair",
        method="POST",
        json={"id": "not-an-id", "symbol": "BTC/USDT", "side": "long", "action": "recheck"},
    ):
        g.user_id = 1
        response, status = inspect.unwrap(routes.repair_position_ownership_route)()

    assert status == 400
    assert response.get_json()["msg"] == "positionOwnership.invalidRepairRequest"


@pytest.mark.parametrize(
    ("market_type", "exchange_id", "available"),
    [
        ("spot", "binance", True),
        ("swap", "okx", True),
        ("spot", "alpaca", False),
        ("USStock", "alpaca", False),
    ],
)
def test_ownership_read_reports_supported_coexistence_markets(
    app, monkeypatch, market_type, exchange_id, available
):
    monkeypatch.setattr(
        routes,
        "_load_ownership_rows",
        lambda *_args, **_kwargs: ([], {
            "market_type": market_type,
            "credential_id": 3,
            "exchange": {"exchange_id": exchange_id},
        }),
    )
    with app.test_request_context("/api/strategies/position-ownership?id=1"):
        g.user_id = 1
        response = inspect.unwrap(routes.get_position_ownership)()

    assert response.get_json()["data"]["advanced_coexistence_available"] is available
