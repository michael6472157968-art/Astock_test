"""Protected manual-position baselines and user repair actions."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from flask import g, jsonify, request

from app.routes.strategy_blueprint import strategy_blp
from app.routes.strategy_services import get_strategy_service
from app.utils.auth import login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _ownership_context(strategy_id: int, user_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], int, str, set[str]]:
    strategy = get_strategy_service().get_strategy(int(strategy_id), user_id=int(user_id))
    if not strategy:
        raise LookupError("strategyV2.strategyNotFound")
    trading = strategy.get("trading_config") if isinstance(strategy.get("trading_config"), dict) else {}
    exchange = strategy.get("exchange_config") if isinstance(strategy.get("exchange_config"), dict) else {}
    from app.services.exchange_execution import resolve_exchange_config
    from app.services.live_trading.leg_context import credential_id_from_exchange_config
    from app.services.live_trading.position_ownership import normalize_market_type
    from app.services.live_trading.records import strategy_allowed_symbols

    resolved = resolve_exchange_config(exchange, user_id=int(user_id))
    credential_id = int(
        credential_id_from_exchange_config(resolved)
        or credential_id_from_exchange_config(exchange)
        or 0
    )
    market_type = normalize_market_type(
        str(trading.get("market_type") or strategy.get("market_type") or resolved.get("market_type") or "swap")
    )
    allowed = strategy_allowed_symbols({"symbol": strategy.get("symbol"), "trading_config": trading})
    return strategy, resolved, credential_id, market_type, allowed


def _load_ownership_rows(strategy_id: int, user_id: int, *, fresh: bool = False):
    strategy, resolved, credential_id, market_type, allowed = _ownership_context(strategy_id, user_id)
    from app.services.live_trading.account_positions import (
        list_account_positions,
        list_strategy_allocations_for_account,
    )
    from app.services.live_trading.position_ownership import (
        build_ownership_rows,
        canonical_symbol,
        list_reservations,
    )
    from app.services.live_trading.strategy_position_sync import sync_strategy_positions_from_exchange

    if str(strategy.get("execution_mode") or "").lower() == "live":
        if fresh:
            from app.services.pending_orders.position_sync_cache import (
                invalidate_position_sync_snapshot_for_exchange,
            )

            invalidate_position_sync_snapshot_for_exchange(
                user_id=int(user_id),
                exchange_id=str(resolved.get("exchange_id") or ""),
                market_type=market_type,
                exchange_config=resolved,
            )
        sync_strategy_positions_from_exchange(int(strategy_id))
    account_rows = list_account_positions(
        user_id=int(user_id), credential_id=credential_id or None, market_type=market_type
    )
    allocated_rows = list_strategy_allocations_for_account(
        user_id=int(user_id), credential_id=credential_id, market_type=market_type,
        allowed_symbols=allowed,
    )
    allowed_canonical = {canonical_symbol(symbol) for symbol in allowed}
    if allowed_canonical:
        account_rows = [
            row for row in account_rows
            if canonical_symbol(str(row.get("symbol") or "")) in allowed_canonical
        ]
    reservations = list_reservations(
        user_id=int(user_id), credential_id=credential_id, market_type=market_type
    )
    if allowed_canonical:
        reservations = [
            row for row in reservations
            if canonical_symbol(str(row.get("symbol_canonical") or row.get("symbol") or "")) in allowed_canonical
        ]
    rows = build_ownership_rows(
        account_rows=account_rows,
        allocated_rows=allocated_rows,
        reservation_rows=reservations,
    )
    return rows, {
        "strategy": strategy,
        "exchange": resolved,
        "credential_id": credential_id,
        "market_type": market_type,
        "allowed": allowed_canonical,
    }


@strategy_blp.route('/strategies/position-ownership', methods=['GET'])
@login_required
def get_position_ownership():
    strategy_id = request.args.get("id", type=int)
    if not strategy_id:
        return jsonify({"code": 0, "msg": "positionOwnership.missingStrategyId", "data": {"items": []}}), 400
    try:
        from app.services.live_trading.position_ownership import supports_position_coexistence

        rows, context = _load_ownership_rows(int(strategy_id), int(g.user_id))
        status = "drift_blocked" if any(row.get("status") == "drift_blocked" for row in rows) else "ok"
        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {
                "items": rows,
                "status": status,
                "market_type": context["market_type"],
                "credential_id": context["credential_id"],
                "advanced_coexistence_available": supports_position_coexistence(
                    context["market_type"],
                    str(context["exchange"].get("exchange_id") or ""),
                ),
            },
        })
    except LookupError:
        return jsonify({"code": 0, "msg": "strategyV2.strategyNotFound", "data": {"items": []}}), 404
    except Exception:
        logger.exception("get_position_ownership failed")
        return jsonify({"code": 0, "msg": "positionOwnership.loadFailed", "data": {"items": []}}), 500


@strategy_blp.route('/strategies/position-ownership/repair', methods=['POST'])
@login_required
def repair_position_ownership_route():
    payload = dict(request.get_json(silent=True) or {})
    try:
        strategy_id = int(payload.get("id") or payload.get("strategy_id") or 0)
    except (TypeError, ValueError):
        strategy_id = 0
    symbol = str(payload.get("symbol") or "").strip()
    side = str(payload.get("side") or "").strip().lower()
    action = str(payload.get("action") or "recheck").strip().lower()
    if not strategy_id or not symbol or side not in {"long", "short"}:
        return jsonify({"code": 0, "msg": "positionOwnership.invalidRepairRequest", "data": None}), 400
    try:
        rows, context = _load_ownership_rows(strategy_id, int(g.user_id), fresh=True)
        from app.services.live_trading.position_ownership import canonical_symbol, repair_position_ownership

        wanted = canonical_symbol(symbol)
        if context["allowed"] and wanted not in context["allowed"]:
            return jsonify({"code": 0, "msg": "positionOwnership.symbolNotOwned", "data": None}), 409
        current = next(
            (row for row in rows if canonical_symbol(row.get("symbol") or "") == wanted and row.get("side") == side),
            None,
        ) or {"account_qty": 0.0, "strategy_qty": 0.0, "inst_id": ""}
        result = repair_position_ownership(
            user_id=int(g.user_id),
            credential_id=int(context["credential_id"] or 0),
            exchange_id=str(context["exchange"].get("exchange_id") or ""),
            market_type=str(context["market_type"]),
            symbol=wanted,
            side=side,
            account_qty=float(current.get("account_qty") or 0.0),
            strategy_qty=float(current.get("strategy_qty") or 0.0),
            action=action,
            inst_id=str(current.get("inst_id") or ""),
        )
        return jsonify({"code": 1, "msg": "success", "data": result.metadata()})
    except LookupError:
        return jsonify({"code": 0, "msg": "strategyV2.strategyNotFound", "data": None}), 404
    except ValueError:
        return jsonify({"code": 0, "msg": "positionOwnership.invalidRepairRequest", "data": None}), 409
    except Exception:
        logger.exception("repair_position_ownership failed")
        return jsonify({"code": 0, "msg": "positionOwnership.repairFailed", "data": None}), 500
