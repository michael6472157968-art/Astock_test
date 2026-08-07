import numpy as np
import pandas as pd
import pytest

talib = pytest.importorskip("talib")

from app.services.factors.talib_adapter import (
    assert_talib_catalog_ready,
    compute_talib_factor,
    compute_talib_indicator,
    list_talib_factors,
)


def _frame():
    close = np.linspace(100.0, 150.0, 200)
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.linspace(1000, 2000, 200),
    })


def test_talib_catalog_exposes_at_least_129_functions():
    assert assert_talib_catalog_ready() >= 129
    ids = {item["factor_id"] for item in list_talib_factors()}
    assert {"talib:RSI", "talib:MACD", "talib:CDLDOJI"} <= ids


def test_talib_single_and_multi_output_computation():
    frame = _frame()
    rsi = compute_talib_factor("RSI", frame, {"timeperiod": 14})
    macd = compute_talib_indicator("MACD", frame)

    assert np.isfinite(rsi)
    assert list(macd.columns) == ["macd", "macdsignal", "macdhist"]
    assert np.isfinite(macd["macdhist"].dropna().iloc[-1])
