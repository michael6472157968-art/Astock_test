import json

from app.services.community_kpis import summarise_backtest_runs


def test_summary_reads_strategy_v2_annualized_return():
    summary = summarise_backtest_runs([
        {
            "id": 7,
            "symbol": "BTC/USDT",
            "timeframe": "1m",
            "result_json": json.dumps({
                "totalReturn": 0.94,
                "annualizedReturn": 12.07,
                "sharpeRatio": 1.79,
                "maxDrawdown": -1.62,
                "totalTrades": 8,
            }),
        }
    ])

    assert summary["total_return"] == 0.94
    assert summary["annual_return"] == 12.07


def test_summary_keeps_legacy_annual_return_fields_compatible():
    camel_case = summarise_backtest_runs([
        {"id": 1, "result_json": json.dumps({"annualReturn": 8.5})}
    ])
    snake_case = summarise_backtest_runs([
        {"id": 2, "result_json": json.dumps({"annual_return": 6.25})}
    ])

    assert camel_case["annual_return"] == 8.5
    assert snake_case["annual_return"] == 6.25
