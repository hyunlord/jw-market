from __future__ import annotations

from jw_chat_agent_poc.orchestrator.bq_mixed_analysis import (
    build_file_market_analysis_call,
)
from jw_chat_agent_poc.orchestrator.bq_runtime_guard import (
    validate_bq_analysis_call,
)


def test_file_market_analysis_binds_both_sources_without_aggregation() -> None:
    call = build_file_market_analysis_call(
        [_market_call()],
        "동아제약 매출 합계는 21,978,584,141원이며 적용 행 수는 348건입니다.",
    )

    assert call is not None
    validate_bq_analysis_call(call)
    data = call["render_data"]
    assert data["contract_id"] == "FILE_MARKET_COMPARISON"
    assert data["source_labels"] == ["FILE", "UBIST"]
    assert data["never_aggregate_sources"] is True
    assert data["fusion_mode"] == "side_by_side"
    assert {row["source"] for row in data["evidence_ledger"]} == {"FILE", "UBIST"}
    assert set(data["evidence_refs"]) <= {
        reference
        for row in data["evidence_ledger"]
        for reference in row.get("references", [])
    }


def test_file_market_analysis_requires_deterministic_file_evidence() -> None:
    assert build_file_market_analysis_call([_market_call()], "") is None


def test_file_market_analysis_requires_concrete_market_evidence() -> None:
    assert build_file_market_analysis_call([], "파일 값") is None


def test_file_market_analysis_rejects_non_market_source() -> None:
    call = _market_call()
    call["source"] = "cache"
    call["render_data"] = {
        **call["render_data"],
        "query_spec": {"source": "cache"},
    }

    assert build_file_market_analysis_call([call], "파일 값") is None


def _market_call() -> dict[str, object]:
    return {
        "source": "UBIST",
        "tool": "get_brand_metric",
        "render_data": {
            "brand": "동아제약",
            "query_spec": {"source": "ubist"},
            "brand_value_series_10pt": [
                {"period": "2026-05", "value_krw": 80_385_988_000},
            ],
        },
    }
