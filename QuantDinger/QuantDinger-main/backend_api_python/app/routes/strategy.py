"""Canonical strategy deployment and lifecycle routes."""

from __future__ import annotations

import os
import re
import time
from typing import Any

from flask import g, jsonify, request

from app import get_trading_executor
from app.routes.strategy_blueprint import strategy_blp
from app.routes.strategy_services import get_strategy_service
from app.services.ai_generation_contracts import (
    SCRIPT_STRATEGY_REPAIR_REQUIREMENTS,
    SCRIPT_STRATEGY_SYSTEM_PROMPT,
)
from app.services.strategy import redact_strategy_row
from app.services.strategy_daily_pnl import load_strategy_daily_metrics
from app.services.strategy_runtime.health import load_runtime_health
from app.services.strategy_v2 import compile_strategy_v2
from app.utils.auth import login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)

# Split route modules share this blueprint.
from app.routes import script_source_routes  # noqa: E402,F401
from app.routes import strategy_account_routes  # noqa: E402,F401
from app.routes import strategy_asset_routes  # noqa: E402,F401
from app.routes import strategy_deviation_routes  # noqa: E402,F401
from app.routes import strategy_executor_routes  # noqa: E402,F401
from app.routes import strategy_grid_routes  # noqa: E402,F401
from app.routes import strategy_ledger_routes  # noqa: E402,F401
from app.routes import strategy_logs_routes  # noqa: E402,F401
from app.routes import strategy_notifications  # noqa: E402,F401
from app.routes import strategy_positions_routes  # noqa: E402,F401
from app.routes import strategy_position_ownership_routes  # noqa: E402,F401
from app.routes import strategy_review_routes  # noqa: E402,F401


def _ok(data: Any = None, message: str = "common.success"):
    return jsonify({"code": 1, "msg": message, "data": data})


def _error(message: str, status: int = 400, data: Any = None):
    return jsonify({"code": 0, "msg": message, "data": data}), status


def _strategy(strategy_id: int):
    return get_strategy_service().get_strategy(int(strategy_id), user_id=int(g.user_id))


def _attach_runtime_health(rows, *, user_id: int | None = None, client_timezone: str = ""):
    items = [dict(row) for row in (rows or [])]
    statuses = {
        int(row.get("id") or 0): str(row.get("status") or "")
        for row in items
        if int(row.get("id") or 0) > 0
    }
    health = load_runtime_health(statuses, strategy_statuses=statuses)
    for row in items:
        row["runtime_health"] = health.get(int(row.get("id") or 0), {})
    if user_id is not None:
        metrics = load_strategy_daily_metrics(
            items,
            user_id=int(user_id),
            client_timezone=str(client_timezone or ""),
        )
        for row in items:
            row.update(metrics.get(int(row.get("id") or 0), {}))
    return items


@strategy_blp.route("/strategies", methods=["GET"])
@login_required
def list_strategies():
    user_id = int(g.user_id)
    rows = get_strategy_service().list_strategies(user_id=user_id)
    enriched = _attach_runtime_health(
        rows,
        user_id=user_id,
        client_timezone=request.headers.get("X-App-Timezone", ""),
    )
    return _ok([redact_strategy_row(row) for row in enriched])


@strategy_blp.route("/strategies/<int:strategy_id>", methods=["GET"])
@login_required
def get_strategy(strategy_id: int):
    row = _strategy(strategy_id)
    if not row:
        return _error("strategyV2.strategyNotFound", 404)
    return _ok(redact_strategy_row(_attach_runtime_health(
        [row],
        user_id=int(g.user_id),
        client_timezone=request.headers.get("X-App-Timezone", ""),
    )[0]))


@strategy_blp.route("/strategies", methods=["POST"])
@login_required
def create_strategy():
    try:
        payload = dict(request.get_json() or {})
        payload["user_id"] = int(g.user_id)
        strategy_id = get_strategy_service().create_strategy(payload)
        return _ok({"id": strategy_id}, "strategyV2.created")
    except Exception as exc:
        logger.warning("strategy create failed: %s", exc)
        return _error(str(exc))


@strategy_blp.route("/strategies/<int:strategy_id>", methods=["PUT"])
@login_required
def update_strategy(strategy_id: int):
    try:
        changed = get_strategy_service().update_strategy(
            strategy_id,
            dict(request.get_json() or {}),
            user_id=int(g.user_id),
        )
        if not changed:
            return _error("strategyV2.strategyNotFound", 404)
        return _ok({"id": strategy_id}, "strategyV2.updated")
    except Exception as exc:
        logger.warning("strategy update failed: %s", exc)
        return _error(str(exc))


@strategy_blp.route("/strategies/<int:strategy_id>", methods=["DELETE"])
@login_required
def delete_strategy(strategy_id: int):
    if get_trading_executor().is_running(strategy_id):
        return _error("strategyV2.stopBeforeDelete", 409)
    if not get_strategy_service().delete_strategy(strategy_id, user_id=int(g.user_id)):
        return _error("strategyV2.strategyNotFound", 404)
    return _ok({"id": strategy_id}, "strategyV2.deleted")


@strategy_blp.route("/strategies/<int:strategy_id>/start", methods=["POST"])
@login_required
def start_strategy(strategy_id: int):
    row = _strategy(strategy_id)
    if not row:
        return _error("strategyV2.strategyNotFound", 404)
    service = get_strategy_service()
    if not service.update_strategy_status(strategy_id, "running", user_id=int(g.user_id)):
        return _error("strategyV2.strategyNotFound", 404)
    executor = get_trading_executor()
    if executor.start_strategy(strategy_id):
        timeout = max(0.0, float(os.getenv("STRATEGY_COMMAND_START_WAIT_SEC", "8")))
        running, detail = executor.wait_strategy_running(strategy_id, timeout=timeout)
        if running and detail == "strategyV2.startQueued":
            return _ok({"id": strategy_id, "status": "starting"}, detail), 202
        if running:
            return _ok({"id": strategy_id, "status": "running"}, "strategyV2.started")
        service.update_strategy_status(strategy_id, "stopped", user_id=int(g.user_id))
        return _error(detail or "strategyV2.startFailed", 409)
    service.update_strategy_status(strategy_id, "stopped", user_id=int(g.user_id))
    detail = str(getattr(executor, "_last_start_failure", "") or "")
    return _error(detail or "strategyV2.startFailed", 409)


@strategy_blp.route("/strategies/<int:strategy_id>/stop", methods=["POST"])
@login_required
def stop_strategy(strategy_id: int):
    row = _strategy(strategy_id)
    if not row:
        return _error("strategyV2.strategyNotFound", 404)
    payload = dict(request.get_json(silent=True) or {})
    close_positions = bool(
        payload.get("close_positions")
        or payload.get("closePositions")
        or str(payload.get("mode") or "").strip().lower() in {"close", "flatten", "stop_and_close"}
    )
    result = get_trading_executor().stop_strategy_with_policy(
        strategy_id,
        close_positions=close_positions,
    )
    get_strategy_service().update_strategy_status(strategy_id, "stopped", user_id=int(g.user_id))
    data = {"id": strategy_id, **result}
    if not result.get("success"):
        return _error("strategyV2.stopClosePartialFailure", 409, data=data)
    message = "strategyV2.stoppedAndCloseQueued" if close_positions else "strategyV2.paused"
    return _ok(data, message)


@strategy_blp.route("/strategies/exchange/test", methods=["POST"])
@login_required
def test_exchange_connection():
    result = get_strategy_service().test_exchange_connection(
        dict(request.get_json() or {}),
        user_id=int(g.user_id),
    )
    if result.get("success"):
        return _ok(result.get("data"), str(result.get("message") or "strategyV2.connectionOk"))
    return _error(str(result.get("message") or "strategyV2.connectionFailed"))


@strategy_blp.route("/strategies/verify", methods=["POST"])
@login_required
def verify_strategy():
    code = str((request.get_json() or {}).get("code") or "").strip()
    if not code:
        return _error("strategyV2.codeRequired")
    try:
        program = compile_strategy_v2(code)
        return _ok({"valid": True, "manifest": program.manifest.metadata()})
    except Exception as exc:
        return _error("strategyV2.contractInvalid", data={"valid": False, "error": str(exc)})


@strategy_blp.route("/strategies/generate", methods=["POST"])
@login_required
def generate_strategy():
    payload = dict(request.get_json() or {})
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return _error("strategyV2.promptRequired")
    try:
        from app.services.billing_service import get_billing_service
        from app.services.llm import LLMService

        llm = LLMService()
        if not llm.is_configured():
            return _error("strategyV2.llmNotConfigured")
        accepted, message = get_billing_service().check_and_consume(
            user_id=int(g.user_id),
            feature="ai_code_gen",
            reference_id=f"strategy_generate_{int(g.user_id)}_{int(time.time())}",
        )
        if not accepted:
            return _error(message or "strategyV2.insufficientCredits", 402)
        content = llm.call_llm_api(
            messages=[
                {"role": "system", "content": SCRIPT_STRATEGY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=llm.get_code_generation_model(),
            temperature=0.4,
            use_json_mode=False,
        )
        code = _strip_code_fence(str(content or ""))
        code, program = _compile_or_repair_generated_strategy(llm, prompt, code)
        return _ok({"code": code, "manifest": program.manifest.metadata()})
    except Exception as exc:
        logger.warning("strategy generation failed: %s", exc)
        return _error("strategyV2.generationInvalid", data={"error": str(exc)})


def _strip_code_fence(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:python)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _compile_or_repair_generated_strategy(llm, prompt: str, code: str):
    try:
        return code, compile_strategy_v2(code)
    except Exception as first_error:
        logger.info("repairing invalid generated strategy: %s", first_error)
        repair_prompt = "\n\n".join(
            [
                SCRIPT_STRATEGY_REPAIR_REQUIREMENTS,
                f"Original user request:\n{prompt}",
                f"Validation error:\n{first_error}",
                f"Invalid generated source:\n{code}",
                "Repair the source and return the complete Python source only.",
            ]
        )
        repaired_content = llm.call_llm_api(
            messages=[
                {"role": "system", "content": SCRIPT_STRATEGY_SYSTEM_PROMPT},
                {"role": "user", "content": repair_prompt},
            ],
            model=llm.get_code_generation_model(),
            temperature=0.15,
            use_json_mode=False,
        )
        repaired_code = _strip_code_fence(str(repaired_content or ""))
        return repaired_code, compile_strategy_v2(repaired_code)
