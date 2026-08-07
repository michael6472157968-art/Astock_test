"""Persistence for Strategy API V2 backtest runs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.utils.db import get_db_connection


class StrategyBacktestRepository:
    def persist_run(
        self,
        *,
        user_id: int,
        strategy_id: int | None,
        strategy_name: str,
        source_id: int | None,
        market: str,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        initial_capital: float,
        commission: float,
        slippage: float,
        leverage: float,
        manifest: dict[str, Any],
        params: dict[str, Any],
        result: dict[str, Any],
        code: str,
    ) -> int | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_backtest_runs
                (user_id, strategy_id, source_id, strategy_name, market, symbol, market_type,
                 timeframe, start_date, end_date, initial_capital, commission, slippage, leverage,
                 params_json, manifest_json, engine_version, code_hash, status, error_message,
                 result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', '', ?, NOW())
                """,
                (
                    int(user_id),
                    int(strategy_id) if strategy_id is not None else None,
                    int(source_id) if source_id is not None else 0,
                    str(strategy_name or ""),
                    str(market or ""),
                    str(symbol or ""),
                    str(
                        next(
                            (
                                item.get("market_type")
                                for item in (manifest.get("universe") or {}).get("instruments", [])
                                if item.get("market_type")
                            ),
                            "spot",
                        )
                    ),
                    str(timeframe or ""),
                    str(start_date),
                    str(end_date),
                    float(initial_capital),
                    float(commission),
                    float(slippage),
                    float(leverage),
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(manifest, ensure_ascii=False),
                    str((result.get("engine") or {}).get("version") or "strategy-api-v2"),
                    hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            run_id = int(cur.lastrowid or 0) or None
            if run_id is not None:
                self._persist_details(cur, run_id, user_id, strategy_id, result)
            db.commit()
            cur.close()
        return run_id

    def list_runs(
        self,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
        strategy_id: int | None = None,
        source_id: int | None = None,
        symbol: str = "",
        market: str = "",
        timeframe: str = "",
    ) -> list[dict[str, Any]]:
        where = ["user_id = ?"]
        params: list[Any] = [int(user_id)]
        for clause, value in (
            ("strategy_id = ?", strategy_id),
            ("source_id = ?", source_id),
            ("symbol = ?", symbol),
            ("market = ?", market),
            ("timeframe = ?", timeframe),
        ):
            if value not in (None, ""):
                where.append(clause)
                params.append(value)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT id, user_id, strategy_id, source_id, strategy_name, market, symbol, market_type, timeframe,
                       start_date, end_date, initial_capital, commission, slippage, leverage,
                       params_json, manifest_json, engine_version, code_hash, status, result_json, created_at
                FROM qd_backtest_runs
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [self._hydrate(row, include_result=False) for row in rows]

    def get_run(self, *, user_id: int, run_id: int) -> Optional[dict[str, Any]]:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT id, user_id, strategy_id, source_id, strategy_name, market, symbol, market_type, timeframe,
                       start_date, end_date, initial_capital, commission, slippage, leverage,
                       params_json, manifest_json, engine_version, code_hash, status, result_json, created_at
                FROM qd_backtest_runs
                WHERE id = ? AND user_id = ?
                """,
                (int(run_id), int(user_id)),
            )
            row = cur.fetchone()
            cur.close()
        return self._hydrate(row, include_result=True) if row else None

    @staticmethod
    def _persist_details(cur, run_id: int, user_id: int, strategy_id: int | None, result: dict[str, Any]) -> None:
        trade_rows = [
            (
                run_id,
                int(user_id),
                int(strategy_id) if strategy_id is not None else None,
                index,
                str(trade.get("exit_time") or ""),
                "close",
                str(trade.get("side") or ""),
                float(trade.get("exit_price") or 0),
                float(trade.get("quantity") or 0),
                float(trade.get("profit") or 0),
                float(trade.get("balance") or 0),
                str(trade.get("close_reason") or ""),
                json.dumps(trade, ensure_ascii=False),
            )
            for index, trade in enumerate(result.get("closedTrades") or [], start=1)
        ]
        if trade_rows:
            cur.executemany(
                """
                INSERT INTO qd_backtest_trades
                (run_id, user_id, strategy_id, trade_index, trade_time, trade_type, side,
                 price, amount, profit, balance, reason, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
                """,
                trade_rows,
            )

        equity_rows = [
            (
                run_id,
                index,
                str(point.get("time") or ""),
                float(point.get("value") or 0),
            )
            for index, point in enumerate(result.get("equityCurve") or [], start=1)
        ]
        if equity_rows:
            cur.executemany(
                """
                INSERT INTO qd_backtest_equity_points
                (run_id, point_index, point_time, point_value, created_at)
                VALUES (?, ?, ?, ?, NOW())
                """,
                equity_rows,
            )

    @staticmethod
    def _hydrate(row: dict[str, Any], *, include_result: bool) -> dict[str, Any]:
        item = dict(row)
        try:
            result = json.loads(item.pop("result_json", "") or "{}")
        except (TypeError, ValueError):
            result = {}
        try:
            item["params"] = json.loads(item.pop("params_json", "") or "{}")
        except (TypeError, ValueError):
            item["params"] = {}
        try:
            item["manifest"] = json.loads(item.pop("manifest_json", "") or "{}")
        except (TypeError, ValueError):
            item["manifest"] = {}
        item["total_return"] = result.get("totalReturn")
        item["win_rate"] = result.get("winRate")
        item["total_trades"] = result.get("totalTrades")
        item["total_executions"] = result.get("totalExecutions")
        item["result_status"] = result.get("resultStatus") or "unknown"
        item["data_kind"] = (result.get("dataProvenance") or {}).get("kind") or "unknown"
        item["benchmark_total_return"] = result.get("benchmarkTotalReturn")
        if include_result:
            item["result"] = _normalize_backtest_result(result, item)
        return item


def _normalize_backtest_result(
    result: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    """Backfill detail fields that were not stored by early Strategy API V2 runs."""
    if not isinstance(result, dict):
        return {}

    initial_capital = _number(
        result.get("initialCapital"),
        _number(
            (result.get("executionAssumptions") or {}).get("initialCapital"),
            _number(run.get("initial_capital")),
        ),
    )
    result.setdefault("initialCapital", initial_capital)
    assumptions = result.get("executionAssumptions")
    if not isinstance(assumptions, dict):
        assumptions = {}
        result["executionAssumptions"] = assumptions
    leverage = _number(run.get("leverage"), 1.0)
    assumption_defaults = {
        "initialCapital": initial_capital,
        "startDate": str(run.get("start_date") or ""),
        "endDate": str(run.get("end_date") or ""),
        "leverageEnabled": leverage > 1.0,
        "leverage": leverage,
        "commission": _number(run.get("commission")),
        "slippage": _number(run.get("slippage")),
    }
    backfilled_fields: list[str] = []
    for field, value in assumption_defaults.items():
        if assumptions.get(field) is None:
            assumptions[field] = value
            backfilled_fields.append(f"executionAssumptions.{field}")

    executions = [
        item
        for item in (result.get("executions") or result.get("rawTrades") or [])
        if isinstance(item, dict)
    ]
    curve = result.get("equityCurve")
    if result.get("liquidated") is None and isinstance(curve, list):
        values = [
            _number(point.get("value"))
            for point in curve
            if isinstance(point, dict) and point.get("value") is not None
        ]
        first_insolvent = next((index for index, value in enumerate(values) if value <= 0), None)
        if first_insolvent is not None and (
            values[first_insolvent] < 0 or first_insolvent < len(values) - 1
        ):
            result["legacyInsolventContinuation"] = True
            backfilled_fields.append("legacyInsolventContinuation")
    if isinstance(curve, list) and curve and any(
        not isinstance(point, dict)
        or any(name not in point for name in ("cash", "grossExposure", "netExposure"))
        for point in curve
    ):
        _backfill_equity_curve(curve, executions, initial_capital)
        backfilled_fields.append("equityCurve.cashAndExposure")

    ledger = result.get("orderLedger")
    if not isinstance(ledger, list) or (not ledger and executions):
        result["orderLedger"] = _legacy_execution_ledger(executions)
        ledger = result["orderLedger"]
        backfilled_fields.append("orderLedger")

    attribution = result.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {}
        result["attribution"] = attribution

    if attribution.get("feeDrag") is None:
        total_commission = _number(result.get("totalCommission"))
        if total_commission == 0.0:
            total_commission = sum(_number(item.get("commission")) for item in executions)
        attribution["feeDrag"] = total_commission / initial_capital if initial_capital else 0.0
        backfilled_fields.append("attribution.feeDrag")

    order_status = attribution.get("orderStatus")
    if not isinstance(order_status, dict) or not any(
        name in order_status for name in ("filled", "partial", "deferred", "rejected")
    ):
        attribution["orderStatus"] = _order_status_counts(ledger, executions)
        backfilled_fields.append("attribution.orderStatus")

    if backfilled_fields:
        compatibility = result.get("compatibility")
        if not isinstance(compatibility, dict):
            compatibility = {}
            result["compatibility"] = compatibility
        compatibility["legacyBackfill"] = True
        compatibility["backfilledFields"] = backfilled_fields

    return result


def _backfill_equity_curve(
    curve: list[Any],
    executions: list[dict[str, Any]],
    initial_capital: float,
) -> None:
    ordered_executions = sorted(
        (item for item in executions if isinstance(item, dict)),
        key=lambda item: _time_key(item.get("time") or item.get("eventTime")),
    )
    cash = float(initial_capital)
    execution_index = 0

    for point in curve:
        if not isinstance(point, dict):
            continue
        point_time = _time_key(point.get("time"))
        while execution_index < len(ordered_executions):
            execution = ordered_executions[execution_index]
            if _time_key(execution.get("time") or execution.get("eventTime")) > point_time:
                break
            side = str(execution.get("side") or "").strip().lower()
            quantity = abs(_number(execution.get("quantity") or execution.get("filledQuantity")))
            price = _number(execution.get("price"))
            notional = abs(_number(execution.get("notional")))
            if notional <= 0.0:
                notional = quantity * price
            commission = abs(_number(execution.get("commission")))
            if side == "buy":
                cash -= notional + commission
            elif side == "sell":
                cash += notional - commission
            execution_index += 1

        value = _number(point.get("value"))
        point.setdefault("cash", round(cash, 8))
        net_market_value = value - _number(point.get("cash"))
        net_exposure = net_market_value / value if value else 0.0
        point.setdefault("netExposure", net_exposure)

        # Legacy results did not retain per-symbol marks, so absolute net exposure
        # is the only recoverable gross-exposure estimate for mixed books.
        point.setdefault("grossExposure", abs(net_exposure))


def _legacy_execution_ledger(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for index, execution in enumerate(executions, start=1):
        if not isinstance(execution, dict):
            continue
        quantity = abs(_number(execution.get("quantity")))
        status = str(execution.get("status") or "filled").strip().lower()
        if status not in {"filled", "partial", "deferred", "rejected"}:
            status = "filled"
        ledger.append({
            "orderId": str(execution.get("order_id") or f"legacy-execution-{index}"),
            "symbol": str(execution.get("symbol") or ""),
            "kind": str(execution.get("type") or "legacy_execution"),
            "value": _number(execution.get("notional")),
            "reason": str(execution.get("reason") or "strategy"),
            "status": status,
            "statusReason": "legacy_execution_record",
            "signalTime": str(execution.get("signal_time") or execution.get("time") or ""),
            "eventTime": str(execution.get("time") or ""),
            "attempt": 1,
            "requestedQuantity": quantity,
            "filledQuantity": quantity,
            "price": _number(execution.get("price")),
            "commission": _number(execution.get("commission")),
        })
    return ledger


def _order_status_counts(
    ledger: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> dict[str, int]:
    names = ("filled", "partial", "deferred", "rejected")
    if ledger:
        return {
            name: sum(
                1 for item in ledger
                if isinstance(item, dict) and str(item.get("status") or "").lower() == name
            )
            for name in names
        }
    return {
        "filled": len(executions),
        "partial": 0,
        "deferred": 0,
        "rejected": 0,
    }


def _time_key(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)
    return number if number == number and number not in (float("inf"), float("-inf")) else float(default)


class FactorResearchRepository:
    def persist_run(
        self,
        *,
        user_id: int,
        source_id: int,
        source_name: str,
        market: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        factor_id: str,
        groups: int,
        holding_period: int,
        commission: float,
        slippage: float,
        neutralize_industry: bool,
        manifest: dict[str, Any],
        result: dict[str, Any],
        code: str,
    ) -> int | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_factor_research_runs
                (user_id, source_id, source_name, market, timeframe, start_date, end_date,
                 factor_id, groups_count, holding_period, commission, slippage,
                 neutralize_industry, universe_size, manifest_json, code_hash, result_json,
                 status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', '', NOW())
                """,
                (
                    int(user_id), int(source_id), str(source_name or ""), str(market or ""),
                    str(timeframe or ""), str(start_date), str(end_date), str(factor_id or ""),
                    int(groups), int(holding_period), float(commission), float(slippage),
                    bool(neutralize_industry), int(result.get("symbolsUsed") or 0),
                    json.dumps(manifest, ensure_ascii=False),
                    hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            run_id = int(cur.lastrowid or 0) or None
            db.commit()
            cur.close()
        return run_id

    def list_runs(
        self,
        *,
        user_id: int,
        source_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["user_id = ?"]
        params: list[Any] = [int(user_id)]
        if source_id is not None:
            where.append("source_id = ?")
            params.append(int(source_id))
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT id, user_id, source_id, source_name, market, timeframe, start_date,
                       end_date, factor_id, groups_count, holding_period, commission, slippage,
                       neutralize_industry, universe_size, manifest_json, code_hash, result_json,
                       status, created_at
                FROM qd_factor_research_runs
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [self._hydrate(row, include_result=False) for row in rows]

    def get_run(self, *, user_id: int, run_id: int) -> Optional[dict[str, Any]]:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT id, user_id, source_id, source_name, market, timeframe, start_date,
                       end_date, factor_id, groups_count, holding_period, commission, slippage,
                       neutralize_industry, universe_size, manifest_json, code_hash, result_json,
                       status, created_at
                FROM qd_factor_research_runs
                WHERE id = ? AND user_id = ?
                """,
                (int(run_id), int(user_id)),
            )
            row = cur.fetchone()
            cur.close()
        return self._hydrate(row, include_result=True) if row else None

    @staticmethod
    def _hydrate(row: dict[str, Any], *, include_result: bool) -> dict[str, Any]:
        item = dict(row)
        try:
            result = json.loads(item.pop("result_json", "") or "{}")
        except (TypeError, ValueError):
            result = {}
        try:
            item["manifest"] = json.loads(item.pop("manifest_json", "") or "{}")
        except (TypeError, ValueError):
            item["manifest"] = {}
        item["rank_ic"] = result.get("rankIc")
        item["icir"] = result.get("icir")
        item["coverage"] = result.get("coverage")
        item["net_long_short_return"] = result.get("netLongShortReturn")
        item["observation_count"] = len(result.get("icSeries") or [])
        if include_result:
            item["result"] = result
        return item
