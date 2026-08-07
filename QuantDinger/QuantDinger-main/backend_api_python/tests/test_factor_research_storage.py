import json

from app.services.strategy_v2.storage import FactorResearchRepository


def test_factor_research_history_hydrates_summary_and_full_result():
    result = {
        "rankIc": 0.12,
        "icir": 1.4,
        "coverage": 0.95,
        "netLongShortReturn": 0.08,
        "icSeries": [{"time": "2026-01-01", "value": 0.12}],
    }
    row = {
        "id": 7,
        "manifest_json": json.dumps({"strategyType": "portfolio"}),
        "result_json": json.dumps(result),
    }

    summary = FactorResearchRepository._hydrate(dict(row), include_result=False)
    detail = FactorResearchRepository._hydrate(dict(row), include_result=True)

    assert summary["rank_ic"] == 0.12
    assert summary["observation_count"] == 1
    assert "result" not in summary
    assert detail["manifest"]["strategyType"] == "portfolio"
    assert detail["result"] == result
