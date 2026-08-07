"""Machine-readable Strategy API V2 authoring contract for external agents."""

from __future__ import annotations

from typing import Any

from app.services.ai_generation_contracts import SCRIPT_STRATEGY_SYSTEM_PROMPT


_STARTER_TEMPLATE = '''"""
Daily Moving Average Regime
Trades a long-only SPY regime from completed daily bars with bounded exposure.
"""

# @param period int 20 Moving-average period range=5:100:5
# @param target_pct float 0.5 Target portfolio weight range=0.1:1.0:0.05


def initialize(context):
    context.set_universe(["USStock:SPY"])
    context.subscribe(frequency="1d", fields=["close"])
    context.set_warmup(22)
    context.set_metadata(direction_mode="long_only", strategy_family="trend")


def handle_data(context, data):
    period = int(context.params.get("period", 20))
    target_pct = float(context.params.get("target_pct", 0.5))
    bars = get_history(period + 1, "1d", "close", "USStock:SPY")
    if len(bars) < period:
        return
    close = float(bars["close"].iloc[-1])
    average = float(bars["close"].tail(period).mean())
    order_target_percent("USStock:SPY", target_pct if close > average else 0.0)
'''


def get_strategy_authoring_contract() -> dict[str, Any]:
    """Return the canonical source-ownership and runtime API contract."""
    return {
        "version": "strategy-api-v2-source-owned-2026-07",
        "doc": "docs/trading/STRATEGY_DEV_GUIDE.md",
        "workflow": [
            "1. Fetch this contract before generating Strategy API V2 source.",
            "2. Generate complete Python source; never send natural language as code.",
            "3. Compile with /api/agent/v1/strategy-sources/compile and repair every validation error.",
            "4. Save the validated draft with /api/agent/v1/strategy-sources.",
            "5. Backtest the saved or validated source before creating a stopped deployment.",
        ],
        "ownership": {
            "source": [
                "universe",
                "market",
                "instrument_type",
                "frequency",
                "subscriptions",
                "direction",
                "sizing",
                "entries",
                "exits",
                "risk",
                "schedules",
            ],
            "run_panel": ["initial_capital", "date_range", "permitted_swap_leverage"],
        },
        "required": [
            "A metadata docstring whose first non-empty line is the strategy name",
            "initialize(context) with context.set_universe(...) and context.subscribe(...) calls",
            "At least one executable handler or registered schedule callback",
            "Canonical instruments such as Crypto:SOL/USDT@spot or USStock:SPY",
        ],
        "forbidden": [
            "get_current_data; use data.current(symbol, field='close')",
            "Position.quantity or Position.cost_basis; use amount and avg_cost",
            "context.run_daily/context.run_weekly/context.run_monthly; schedule helpers are global",
            "context.params reads inside initialize(context)",
            "Run-panel overrides for symbol, market type, frequency, or leverage permission",
            "File, network, process, reflection, or dynamic execution APIs",
        ],
        "system_contract": SCRIPT_STRATEGY_SYSTEM_PROMPT,
        "starter_template": _STARTER_TEMPLATE,
    }
