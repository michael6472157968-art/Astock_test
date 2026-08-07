"""Protected manual inventory and ownership-drift calculations."""

import pytest

from app.services.pending_orders import entry_position_guard
from app.services.live_trading.account_positions import reconcile_strategy_vs_account
from app.services.live_trading.position_ownership import (
    ADVANCED_MODE,
    STATUS_BLOCKED,
    STATUS_OK,
    calculate_position_ownership,
    repair_position_ownership,
    supports_position_coexistence,
)


def test_advanced_manual_baseline_allows_matching_account_position():
    snapshot = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.025,
        strategy_qty=0.015,
        protected_qty=0.01,
        coexistence_mode=ADVANCED_MODE,
    )
    assert snapshot.status == STATUS_OK
    assert snapshot.allowed is True
    assert snapshot.unknown_qty == pytest.approx(0.0)


@pytest.mark.parametrize("market_type", ["spot", "swap", "future", "perpetual"])
def test_crypto_spot_and_derivative_markets_support_position_coexistence(market_type):
    assert supports_position_coexistence(market_type, "binance") is True


def test_non_crypto_market_rejects_advanced_position_coexistence():
    assert supports_position_coexistence("USStock") is False
    assert supports_position_coexistence("spot", "alpaca") is False
    with pytest.raises(ValueError, match="positionOwnership.coexistenceMarketUnsupported"):
        repair_position_ownership(
            user_id=1,
            credential_id=2,
            exchange_id="alpaca",
            market_type="USStock",
            symbol="AAPL",
            side="long",
            account_qty=1,
            strategy_qty=0,
            action="protect_manual",
        )


def test_strict_mode_blocks_unallocated_manual_position_once():
    first = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.01,
        strategy_qty=0.0,
    )
    duplicate = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.01,
        strategy_qty=0.0,
        previous_status=first.status,
        previous_reason=first.reason,
    )
    assert first.status == STATUS_BLOCKED
    assert first.should_log is True
    assert duplicate.should_log is False


def test_manual_reduction_below_protected_total_blocks_entries():
    snapshot = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.015,
        strategy_qty=0.015,
        protected_qty=0.01,
        coexistence_mode=ADVANCED_MODE,
    )
    assert snapshot.status == STATUS_BLOCKED
    assert snapshot.reason == "account_below_protected_allocation"
    assert snapshot.unknown_qty == pytest.approx(-0.01)


def test_account_reconciliation_counts_protected_manual_inventory():
    result = reconcile_strategy_vs_account(
        local_rows=[{"symbol": "BTC/USDT", "side": "long", "size": 0.015}],
        account_rows=[{"symbol": "BTC/USDT", "side": "long", "size": 0.025}],
        allocated_rows=[{"symbol": "BTC/USDT", "side": "long", "size": 0.015}],
        protected_rows=[{
            "symbol_canonical": "BTC/USDT",
            "side": "long",
            "coexistence_mode": "advanced",
            "manual_reserved_qty": 0.01,
        }],
    )
    assert result["status"] == "ok"
    assert result["strategy_allocations"][0]["protected_size"] == pytest.approx(0.01)


def test_entry_guard_returns_structured_drift_without_checking_opposite_leg(monkeypatch):
    snapshot = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.025,
        strategy_qty=0.015,
    )
    monkeypatch.setattr(entry_position_guard, "fetch_allocated_position_size", lambda **_kwargs: 0.015)
    monkeypatch.setattr(entry_position_guard, "evaluate_and_record_ownership", lambda **_kwargs: snapshot)
    monkeypatch.setattr(
        entry_position_guard,
        "fetch_position_size_for_side",
        lambda *_args, **_kwargs: pytest.fail("blocked ownership must stop before opposite-leg checks"),
    )

    result = entry_position_guard.evaluate_entry_position_guard(
        client=object(),
        strategy_id=1,
        user_id=2,
        credential_id=3,
        exchange_id="binance",
        market_type="swap",
        symbol="BTC/USDT",
        side="long",
        strategy_config={},
        exchange_config={},
        account_qty=0.025,
    )

    assert result.error.startswith("position_drift_detected:")
    assert "account=0.025" in result.error
    assert "strategy=0.015" in result.error
    assert result.log_level == "error"
    assert result.ownership["status"] == STATUS_BLOCKED


def test_spot_entry_guard_applies_ownership_without_opposite_leg_check(monkeypatch):
    snapshot = calculate_position_ownership(
        symbol="BTC/USDT",
        side="long",
        account_qty=0.025,
        strategy_qty=0.015,
        protected_qty=0.01,
        coexistence_mode=ADVANCED_MODE,
    )
    monkeypatch.setattr(entry_position_guard, "fetch_allocated_position_size", lambda **_kwargs: 0.015)
    monkeypatch.setattr(entry_position_guard, "evaluate_and_record_ownership", lambda **_kwargs: snapshot)
    monkeypatch.setattr(
        entry_position_guard,
        "fetch_position_size_for_side",
        lambda *_args, **_kwargs: pytest.fail("spot must not inspect a short exchange leg"),
    )

    result = entry_position_guard.evaluate_entry_position_guard(
        client=object(),
        strategy_id=1,
        user_id=2,
        credential_id=3,
        exchange_id="binance",
        market_type="spot",
        symbol="BTC/USDT",
        side="long",
        strategy_config={"direction_mode": "long_only"},
        exchange_config={},
        account_qty=0.025,
    )

    assert result.error == ""
    assert result.ownership["coexistence_mode"] == ADVANCED_MODE
    assert result.ownership["status"] == STATUS_OK
