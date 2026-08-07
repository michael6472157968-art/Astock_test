from app.services.grid.exchange_orders import normalize_grid_order_quantity
from app.services.live_trading.gate import GateUsdtFuturesClient
from app.services.live_trading.htx import HtxClient


def _gate_client(order_size_min="1", multiplier="0.0001"):
    client = GateUsdtFuturesClient.__new__(GateUsdtFuturesClient)
    client.get_contract = lambda **_kwargs: {
        "order_size_min": order_size_min,
        "quanto_multiplier": multiplier,
    }
    return client


def test_gate_futures_never_rounds_sub_minimum_quantity_up():
    client = _gate_client()

    size, headers = client._resolve_order_size(
        contract="BTC_USDT",
        side="sell",
        base_size=0.00005,
    )

    assert size == "0"
    assert headers is None


def test_gate_grid_quantity_is_floored_and_returned_in_base_units():
    client = _gate_client()

    assert normalize_grid_order_quantity(
        client,
        symbol="BTC/USDT",
        quantity=0.00039,
        market_type="swap",
    ) == 0.0003
    assert normalize_grid_order_quantity(
        client,
        symbol="BTC/USDT",
        quantity=0.00005,
        market_type="swap",
    ) == 0.0


def test_htx_futures_never_rounds_sub_contract_quantity_up():
    client = HtxClient.__new__(HtxClient)
    client.get_contract_info = lambda **_kwargs: {"contract_size": "0.001"}

    assert client._base_to_contracts(symbol="BTC/USDT", qty=0.0005) == 0
