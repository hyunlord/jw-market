from __future__ import annotations

from scripts.phase3_off_gate import (
    APPROVED_PRESENTATION_EXCEPTIONS,
    _numeric_tokens_comparison,
    _tool_contracts,
)


def test_phase3_tool_contract_ignores_cache_state_only() -> None:
    baseline = {
        "qa_trace": {
            "tools": [
                {
                    "name": "market_scope",
                    "status": "ok",
                    "row_count": 12,
                    "data_as_of": "2026-05",
                    "cache_hit": True,
                    "endpoint": "/api/cause/리바로",
                }
            ]
        }
    }
    candidate = {
        "qa_trace": {
            "tools": [
                {
                    "name": "market_scope",
                    "status": "ok",
                    "row_count": 12,
                    "data_as_of": "2026-05",
                    "cache_hit": False,
                    "endpoint": "/api/cause/리바로",
                }
            ]
        }
    }

    assert _tool_contracts(baseline) == _tool_contracts(candidate)


def test_phase3_tool_contract_still_rejects_data_contract_drift() -> None:
    baseline = {"qa_trace": {"tools": [{"name": "market_scope", "status": "ok", "row_count": 12}]}}
    candidate = {"qa_trace": {"tools": [{"name": "market_scope", "status": "ok", "row_count": 11}]}}

    assert _tool_contracts(baseline) != _tool_contracts(candidate)


def test_phase3_approved_presentation_compares_numeric_multisets() -> None:
    comparison = _numeric_tokens_comparison(
        "owner_brand_share",
        ["3.76", "2026", "05", "3.76"],
        ["05", "3.76", "3.76", "2026"],
    )

    assert comparison["passed"] is True
    assert comparison["mode"] == "approved_presentation_numeric_multiset"


def test_phase3_inherits_all_pl_approved_presentation_exceptions() -> None:
    assert APPROVED_PRESENTATION_EXCEPTIONS == {
        "B-07",
        "C_03",
        "owner_brand_share",
    }

    comparison = _numeric_tokens_comparison(
        "B-07",
        ["10.019", "0.213"],
        ["0.213", "10.019"],
    )

    assert comparison["passed"] is True
    assert comparison["mode"] == "approved_presentation_numeric_multiset"


def test_phase3_nonapproved_case_keeps_ordered_numeric_equality() -> None:
    comparison = _numeric_tokens_comparison("B-01", ["1", "2"], ["2", "1"])

    assert comparison["passed"] is False
    assert comparison["mode"] == "exact"
