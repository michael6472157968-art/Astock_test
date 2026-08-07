"""Preflight position ownership checks for derivative entry orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from app.services.live_trading.position_ownership import (
    evaluate_and_record_ownership,
    normalize_market_type,
    ownership_log_message,
)
from app.services.live_trading.position_query import query_exchange_position_size
from app.services.live_trading.records import (
    fetch_allocated_position_size,
    fetch_position_size_for_side,
)
from app.services.strategy_live_guard import resolve_strategy_direction_mode


@dataclass(frozen=True)
class EntryPositionGuardResult:
    """Result consumed by the pending-order worker without worker side effects."""

    ownership: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    log_level: str = ""
    log_message: str = ""


def strategy_allows_simultaneous_legs(strategy: Dict[str, Any]) -> bool:
    """Return whether one strategy intentionally owns both derivative legs."""
    return resolve_strategy_direction_mode(strategy) in {"both", "neutral"}


def evaluate_entry_position_guard(
    *,
    client: Any,
    strategy_id: int,
    user_id: int,
    credential_id: int,
    exchange_id: str,
    market_type: str,
    symbol: str,
    side: str,
    strategy_config: Dict[str, Any],
    exchange_config: Dict[str, Any],
    account_qty: float,
) -> EntryPositionGuardResult:
    """Validate ownership drift and an optional opposite strategy leg."""
    strategy_qty = fetch_allocated_position_size(
        strategy_id=int(strategy_id),
        credential_id=int(credential_id or 0),
        market_type=str(market_type),
        symbol=str(symbol),
        side=str(side),
    )
    snapshot = evaluate_and_record_ownership(
        user_id=int(user_id or 1),
        credential_id=int(credential_id or 0),
        exchange_id=str(exchange_id or ""),
        market_type=str(market_type),
        symbol=str(symbol),
        side=str(side),
        account_qty=float(account_qty or 0.0),
        strategy_qty=float(strategy_qty or 0.0),
    )
    metadata = snapshot.metadata()
    if not snapshot.allowed:
        error = (
            "position_drift_detected:"
            f"side={side},account={snapshot.account_qty},strategy={snapshot.strategy_qty},"
            f"protected={snapshot.protected_qty},unknown={snapshot.unknown_qty},"
            f"mode={snapshot.coexistence_mode}"
        )
        return EntryPositionGuardResult(
            ownership=metadata,
            error=error,
            log_level="error" if snapshot.should_log else "",
            log_message=ownership_log_message(snapshot) if snapshot.should_log else "",
        )

    # Spot has only the long inventory lane.  Its ownership baseline and drift
    # rules are identical to swap, but there is no opposite exchange leg to
    # inspect before an entry.
    if normalize_market_type(market_type) == "spot":
        return EntryPositionGuardResult(ownership=metadata)

    if strategy_allows_simultaneous_legs(strategy_config):
        return EntryPositionGuardResult(ownership=metadata)

    opposite_side = "short" if str(side) == "long" else "long"
    local_opposite = fetch_position_size_for_side(int(strategy_id), str(symbol), opposite_side)
    if local_opposite <= 1e-8:
        return EntryPositionGuardResult(ownership=metadata)
    try:
        live_opposite = float(
            query_exchange_position_size(
                client=client,
                symbol=str(symbol),
                pos_side=opposite_side,
                market_type=str(market_type),
                exchange_config=exchange_config,
                strict=True,
            )
            or 0.0
        )
    except Exception as exc:
        return EntryPositionGuardResult(
            ownership=metadata,
            error=f"opposite_position_snapshot_failed:{exc}",
        )
    if live_opposite <= 1e-8:
        return EntryPositionGuardResult(ownership=metadata)
    return EntryPositionGuardResult(
        ownership=metadata,
        error=(
            "opposite_position_still_open:"
            f"side={opposite_side},exchange={live_opposite},local={local_opposite}"
        ),
        log_level="warning",
        log_message=f"Reverse entry rejected until the opposite position is fully closed: {symbol}",
    )
