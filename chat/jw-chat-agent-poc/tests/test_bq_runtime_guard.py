from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.bq_runtime_guard import (
    BQAnalysisValidationError,
    validate_bq_analysis_call,
)


def test_valid_bq_analysis_passes_runtime_guard() -> None:
    validate_bq_analysis_call(_analysis())


def test_multi_source_analysis_requires_never_aggregate_marker() -> None:
    call = _analysis()
    call["render_data"]["source_labels"] = ["UBIST", "IQVIA NSA"]

    with pytest.raises(BQAnalysisValidationError, match="never-aggregate"):
        validate_bq_analysis_call(call)


def test_cross_source_analysis_requires_explicit_side_by_side_fusion() -> None:
    call = _analysis()
    call["render_data"]["source_labels"] = ["UBIST", "IQVIA NSA"]
    call["render_data"]["never_aggregate_sources"] = True

    with pytest.raises(BQAnalysisValidationError, match="side-by-side"):
        validate_bq_analysis_call(call)


def test_file_and_market_source_requires_never_aggregate_marker() -> None:
    call = _analysis()
    call["render_data"].update(
        {
            "contract_id": "FILE_MARKET_COMPARISON",
            "source_labels": ["FILE", "UBIST"],
            "fusion_mode": "side_by_side",
            "evidence_refs": ["FILE.deterministic_answer", "UBIST.series"],
            "evidence_ledger": [
                {
                    "source": "FILE",
                    "kind": "file_answer",
                    "identity": "uploaded_file:deterministic_answer",
                    "references": ["FILE.deterministic_answer"],
                },
                {
                    "source": "UBIST",
                    "kind": "series",
                    "identity": "UBIST:2026-05:brand-sales",
                    "references": ["UBIST.series"],
                },
            ],
        }
    )

    with pytest.raises(BQAnalysisValidationError, match="never-aggregate"):
        validate_bq_analysis_call(call)


def test_file_market_analysis_rejects_non_market_source_label() -> None:
    call = _analysis()
    call["render_data"].update(
        {
            "contract_id": "FILE_MARKET_COMPARISON",
            "source_labels": ["FILE", "cache"],
            "market_source_labels": ["cache"],
            "never_aggregate_sources": True,
            "fusion_mode": "side_by_side",
            "evidence_refs": ["FILE.deterministic_answer", "cache.series"],
            "evidence_ledger": [
                {
                    "source": "FILE",
                    "kind": "file_answer",
                    "identity": "uploaded_file:deterministic_answer",
                    "references": ["FILE.deterministic_answer"],
                },
                {
                    "source": "cache",
                    "kind": "series",
                    "identity": "cache:2026-05:brand-sales",
                    "references": ["cache.series"],
                },
            ],
        }
    )

    with pytest.raises(BQAnalysisValidationError, match="concrete market source"):
        validate_bq_analysis_call(call)


def test_numeric_analysis_requires_evidence_ledger() -> None:
    call = _analysis()
    call["render_data"].pop("evidence_ledger")

    with pytest.raises(BQAnalysisValidationError, match="evidence ledger"):
        validate_bq_analysis_call(call)


def test_generic_tool_result_is_not_numeric_evidence() -> None:
    call = _analysis()
    call["render_data"]["evidence_ledger"] = [
        {"source": "UBIST", "kind": "tool_result", "identity": "get_brand_metric:result"}
    ]

    with pytest.raises(BQAnalysisValidationError, match="concrete evidence"):
        validate_bq_analysis_call(call)


def test_chart_reference_must_resolve_to_ledger_row() -> None:
    call = _analysis()
    call["render_data"]["chart_payloads"] = [_chart()]

    with pytest.raises(BQAnalysisValidationError, match="unbound chart evidence"):
        validate_bq_analysis_call(call)


def test_patient_ratio_requires_bound_hira_and_market_inputs() -> None:
    call = _analysis()
    call["render_data"].update(
        {
            "contract_id": "A3",
            "source_labels": ["HIRA", "UBIST"],
            "never_aggregate_sources": True,
            "fusion_mode": "side_by_side",
            "evidence_refs": ["HIRA.render_data.items.ptntCnt"],
            "evidence_ledger": [
                {
                    "source": "HIRA",
                    "kind": "number",
                    "identity": "get_disease_stats:items:ptntCnt:2026",
                    "references": ["HIRA.render_data.items.ptntCnt"],
                },
                {
                    "source": "UBIST",
                    "kind": "series",
                    "identity": "get_brand_metric:brand_value_series_10pt:2026-05",
                    "references": ["UBIST.render_data.brand_value_series_10pt"],
                },
            ],
        }
    )

    with pytest.raises(BQAnalysisValidationError, match="patient-ratio evidence"):
        validate_bq_analysis_call(call)


def test_news_reference_requires_complete_identity() -> None:
    call = _analysis()
    call["render_data"]["news_refs"] = [
        {"title": "기사", "date": "2026-01-01", "source": "매체", "url": ""}
    ]

    with pytest.raises(BQAnalysisValidationError, match="news identity"):
        validate_bq_analysis_call(call)


def test_news_reference_rejects_non_mapping_members() -> None:
    call = _analysis()
    call["render_data"]["news_refs"] = [
        {"title": "기사", "date": "2026-01-01", "source": "매체", "url": "https://example.test/1"},
        "not-a-news-reference",
    ]

    with pytest.raises(BQAnalysisValidationError, match="news identity collection"):
        validate_bq_analysis_call(call)


def test_chart_requires_explicit_scope_and_evidence_refs() -> None:
    call = _analysis()
    call["render_data"]["chart_payloads"] = [
        {
            "chart_type": "line",
            "labels": ["2026-01"],
            "datasets": [{"label": "매출(KRW)", "data": [1.0]}],
            "evidence_refs": ["UBIST.series"],
        }
    ]

    with pytest.raises(BQAnalysisValidationError, match="chart scope"):
        validate_bq_analysis_call(call)


def test_chart_collection_rejects_non_mapping_members() -> None:
    call = _analysis()
    call["render_data"]["chart_payloads"] = [_chart(), "not-a-chart"]

    with pytest.raises(BQAnalysisValidationError, match="chart collection"):
        validate_bq_analysis_call(call)


def test_mixed_chart_requires_both_market_and_file_groups() -> None:
    call = _analysis()
    call["render_data"]["chart_payloads"] = [
        {
            "scope": "MIXED",
            "market": [_chart(scope=None)],
            "file": [],
        }
    ]

    with pytest.raises(BQAnalysisValidationError, match="market and file"):
        validate_bq_analysis_call(call)


def test_internal_identifier_is_rejected_from_analysis_surface() -> None:
    call = _analysis()
    call["render_data"]["insights"] = ["document_id=42 값을 확인했습니다."]

    with pytest.raises(BQAnalysisValidationError, match="internal identifier"):
        validate_bq_analysis_call(call)


def _analysis() -> dict[str, object]:
    return {
        "tool": "bq_analysis",
        "source": "BQ deterministic evidence",
        "summary_text": "UBIST 성장률을 계산했습니다.",
        "render_data": {
            "contract_id": "A1",
            "calculation": "market_growth",
            "insights": ["UBIST 성장률을 계산했습니다."],
            "source_labels": ["UBIST"],
            "evidence_ledger": [
                {"source": "UBIST", "kind": "series", "identity": "UBIST:2026-01:brand-sales"}
            ],
        },
    }


def _chart(*, scope: str | None = "MARKET") -> dict[str, object]:
    chart: dict[str, object] = {
        "chart_type": "line",
        "labels": ["2026-01"],
        "datasets": [{"label": "매출(KRW)", "data": [1.0]}],
        "evidence_refs": ["UBIST.series"],
    }
    if scope is not None:
        chart["scope"] = scope
    return chart
