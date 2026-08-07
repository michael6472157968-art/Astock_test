import numpy as np
import pandas as pd

from app.services.strategy_v2 import StrategyV2BacktestRunner


def test_v2_strategy_can_compute_builtin_factor_without_future_data():
    close = np.linspace(100.0, 130.0, 40)
    frame = pd.DataFrame({
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.full(40, 1000),
    }, index=pd.date_range("2026-01-01", periods=40, freq="D"))
    code = """
def initialize(context):
    context.set_universe(["USStock:AAPL"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    if len(get_history(100, security_list="AAPL")) >= 20:
        context.log("sma=%.2f" % factor("sma", "AAPL", period=20))
"""
    result = StrategyV2BacktestRunner(
        code=code,
        frames={"USStock:AAPL": frame},
        initial_capital=10000,
    ).run()

    assert result["logs"]
    assert result["logs"][0].startswith("sma=")
