from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
)


@pytest.mark.parametrize(
    ("result", "expected_code", "expected_terminal", "expected_partial"),
    (
        (
            {
                "answer": "HIRA 공식 조회에 실패했습니다. 공식 시스템에서 확인해 주세요.",
                "tool_calls": [
                    {
                        "source": "HIRA",
                        "status": "error",
                        "render_data": {"error_code": "UPSTREAM_UNAVAILABLE"},
                    }
                ],
                "sources": ["HIRA"],
            },
            TypedFailureCode.UPSTREAM_UNAVAILABLE,
            True,
            False,
        ),
        (
            {
                "answer": "리바로는 현재 전략 시장 분류에 연결되어 있지 않습니다.",
                "tool_calls": [
                    {
                        "tool": "query_failed",
                        "status": "error",
                        "render_data": {
                            "reason_code": "market_unresolved",
                            "brand": "리바로",
                        },
                    }
                ],
            },
            TypedFailureCode.MARKET_UNRESOLVED,
            True,
            False,
        ),
        (
            {
                "answer": "리바로와 가드렛은 기준이 달라 직접 비교할 수 없습니다.",
                "tool_calls": [
                    {
                        "tool": "query_failed",
                        "status": "error",
                        "render_data": {
                            "reason_code": "incompatible_comparison",
                            "anchor_brand": "리바로",
                            "comparison_brand": "가드렛",
                        },
                    }
                ],
            },
            TypedFailureCode.INCOMPATIBLE_COMPARISON,
            True,
            True,
        ),
    ),
)
def test_normalize_typed_failure_when_existing_typed_state_is_present(
    result: dict[str, object],
    expected_code: TypedFailureCode,
    expected_terminal: bool,
    expected_partial: bool,
) -> None:
    normalized = normalize_typed_failure(result)

    assert normalized is not None
    assert normalized.code is expected_code
    assert normalized.user_message == result["answer"]
    assert normalized.terminal is expected_terminal
    assert normalized.partial is expected_partial


@pytest.mark.parametrize(
    ("result", "expected_code"),
    (
        ({"error_code": "IDENTITY_MISMATCH"}, TypedFailureCode.IDENTITY_MISMATCH),
        ({"reason_code": "INDEX_MISS"}, TypedFailureCode.INDEX_MISS),
        ({"status": "EVIDENCE_BINDING_FAILED"}, TypedFailureCode.EVIDENCE_BINDING_FAILED),
        (
            {"sources": [{"error_code": "UPSTREAM_UNAVAILABLE"}]},
            TypedFailureCode.UPSTREAM_UNAVAILABLE,
        ),
        (
            {
                "router_diagnostics": {
                    "routing_v4": {
                        "executed_call_signature": {
                            "reason_code": "IDENTITY_MISMATCH",
                        }
                    }
                }
            },
            TypedFailureCode.IDENTITY_MISMATCH,
        ),
    ),
)
def test_normalize_typed_failure_when_legacy_surface_varies(
    result: dict[str, object],
    expected_code: TypedFailureCode,
) -> None:
    normalized = normalize_typed_failure(result)

    assert normalized is not None
    assert normalized.code is expected_code


def test_normalize_typed_failure_when_multiple_codes_exist_uses_semantic_priority() -> None:
    result = {
        "answer": "제품 구성이 일치하지 않습니다.",
        "tool_calls": [
            {"render_data": {"error_code": "UPSTREAM_UNAVAILABLE"}},
        ],
        "router_diagnostics": {
            "routing_v4": {
                "official_web_fallback": {"reason_code": "IDENTITY_MISMATCH"},
            }
        },
    }

    normalized = normalize_typed_failure(result)

    assert normalized is not None
    assert normalized.code is TypedFailureCode.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "reserved_code",
    (
        "NO_OFFICIAL_RECORD",
        "NO_EVIDENCE",
        "PARTIAL_EVIDENCE",
        "UNSUPPORTED_MULTI_ENTITY",
        "NO_FILE_ATTACHED",
    ),
)
def test_normalize_typed_failure_when_reserved_code_has_no_exact_producer_returns_none(
    reserved_code: str,
) -> None:
    normalized = normalize_typed_failure({"reason_code": reserved_code})

    assert normalized is None


def test_normalize_typed_failure_preserves_explicit_recovery_and_evidence() -> None:
    normalized = normalize_typed_failure(
        {
            "error_code": "INDEX_MISS",
            "answer": "내부 색인에서 확인하지 못했습니다.",
            "recovery_action": "공식 원천에서 직접 확인해 주세요.",
            "source": "HIRA",
            "evidence_summary": ["내부 색인 조회 0건", "공식 부재는 미확정"],
        }
    )

    assert normalized is not None
    assert normalized.recovery_action == "공식 원천에서 직접 확인해 주세요."
    assert normalized.source == "HIRA"
    assert normalized.evidence_summary == (
        "내부 색인 조회 0건",
        "공식 부재는 미확정",
    )


def test_typed_failure_result_is_immutable() -> None:
    normalized = normalize_typed_failure({"error_code": "INDEX_MISS"})

    assert normalized is not None
    with pytest.raises(FrozenInstanceError):
        setattr(normalized, "terminal", False)


def test_normalize_typed_failure_when_no_exact_code_returns_none() -> None:
    assert normalize_typed_failure(
        {
            "status": "typed_unavailable",
            "answer": "조회하지 못했습니다.",
        }
    ) is None


def test_normalize_typed_failure_does_not_mutate_the_legacy_result() -> None:
    result = {
        "answer": "HIRA 공식 조회에 실패했습니다.",
        "tool_calls": [
            {
                "source": "HIRA",
                "render_data": {"error_code": "UPSTREAM_UNAVAILABLE"},
            }
        ],
    }
    expected = {
        "answer": "HIRA 공식 조회에 실패했습니다.",
        "tool_calls": [
            {
                "source": "HIRA",
                "render_data": {"error_code": "UPSTREAM_UNAVAILABLE"},
            }
        ],
    }

    normalize_typed_failure(result)

    assert result == expected
