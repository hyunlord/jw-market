from __future__ import annotations

import pytest

from jw_chat_agent_poc.tool_use.routing_v4_execution import (
    actionable_official_web_failure,
    execution_failure_reason,
    official_web_fallback_eligible,
    official_web_fallback_policy,
)
from jw_chat_agent_poc.tool_use.contracts import AgentResult
from jw_chat_agent_poc.tool_use.routing_v4_runtime import (
    _explicit_internal_only,
    _web_provider_outcome,
)


@pytest.mark.parametrize(
    "runtime_reason",
    (
        "UPSTREAM_UNAVAILABLE",
        "NO_EVIDENCE",
        "NO_RECORD_FOUND",
        "PARTIAL_RESULT",
    ),
)
def test_non_security_evidence_gaps_are_web_fallback_eligible(
    monkeypatch: pytest.MonkeyPatch,
    runtime_reason: str,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    assert official_web_fallback_eligible(
        source_domain="hira",
        runtime_reason=runtime_reason,
        usable_authoritative_results=1 if runtime_reason == "PARTIAL_RESULT" else 0,
        requested_source_explicit=False,
        missing_requested_facets=("2023",) if runtime_reason == "PARTIAL_RESULT" else (),
    )


def test_internal_only_and_proven_nonexistence_never_enable_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    for exclusions in (
        {"internal_only": True, "authoritative_nonexistence_proven": False},
        {"internal_only": False, "authoritative_nonexistence_proven": True},
    ):
        assert not official_web_fallback_eligible(
            source_domain="hira",
            runtime_reason="NO_RECORD_FOUND",
            usable_authoritative_results=0,
            requested_source_explicit=False,
            missing_requested_facets=(),
            **exclusions,
        )


def test_explicit_source_failure_is_supplemented_without_silent_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=("https://opendata.hira.or.kr/official",),
        requested_source_explicit=True,
    )

    assert decision.web_call_budget == 1
    assert decision.accepted_urls == ("https://opendata.hira.or.kr/official",)
    assert decision.separate_section is True
    assert "요청한 HIRA" in decision.disclosure
    assert "대신한 값이 아니라" in decision.disclosure
    assert "UPSTREAM_UNAVAILABLE" not in decision.disclosure


def test_partial_result_requires_a_missing_requested_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    for usable_authoritative_results in (0, 1):
        assert not official_web_fallback_eligible(
            source_domain="hira",
            runtime_reason="PARTIAL_RESULT",
            usable_authoritative_results=usable_authoritative_results,
            requested_source_explicit=False,
            missing_requested_facets=(),
        )
    assert official_web_fallback_eligible(
        source_domain="hira",
        runtime_reason="PARTIAL_RESULT",
        usable_authoritative_results=1,
        requested_source_explicit=False,
        missing_requested_facets=("2023",),
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("내부 데이터만 사용해줘", True),
        ("사내 자료만 보여줘", True),
        ("보유 데이터로만 답해줘", True),
        ("웹 검색 없이 알려줘", True),
        ("외부 자료 제외하고 답해줘", True),
        ("내부 자료가 아니라 공개 자료로 알려줘", False),
        ("웹 검색 없이가 아니라 웹 검색으로 알려줘", False),
    ),
)
def test_internal_only_requires_an_explicit_non_negated_phrase(
    question: str,
    expected: bool,
) -> None:
    assert _explicit_internal_only(question) is expected


def test_identity_mismatch_reason_survives_execution_normalization() -> None:
    result = AgentResult(
        status="typed_stop",
        answer="연결된 고시의 제품 구성이 요청 브랜드와 다릅니다.",
        tool_calls=(
            {
                "tool": "hira_reimbursement_criteria",
                "source": "HIRA",
                "status": "no_data",
                "render_data": {
                    "error_code": "IDENTITY_MISMATCH",
                    "evidence": [],
                },
            },
        ),
        sources=("HIRA",),
        traces=(),
        fallback_code=None,
    )

    assert execution_failure_reason(result) == "IDENTITY_MISMATCH"


def test_zero_official_results_return_actionable_hira_guidance() -> None:
    guidance = actionable_official_web_failure(
        question="아일리아 급여기준 알려줘",
        source_domain="hira",
        reason_code="NO_RECORD_FOUND",
        provider_outcome="empty",
    )

    assert guidance is not None
    assert "공식 웹 보완 검색을 시도" in guidance
    assert "[HIRA 보험인정기준 검색](https://www.hira.or.kr/" in guidance
    assert "검색어: 아일리아 급여기준" in guidance
    assert "확인할 항목: 제품명, 성분 구성, 고시 시행일" in guidance


def test_provider_timeout_returns_actionable_regulatory_guidance() -> None:
    guidance = actionable_official_web_failure(
        question="아일리아 허가사항 알려줘",
        source_domain="regulatory",
        reason_code="UPSTREAM_UNAVAILABLE",
        provider_outcome="timeout",
    )

    assert guidance is not None
    assert "공식 웹 보완 검색이 5초 안에 완료되지 않았습니다." in guidance
    assert "[식품의약품안전처 의약품 검색](https://nedrug.mfds.go.kr/" in guidance
    assert "답을 추정하지 않고" in guidance


def test_identity_mismatch_returns_navigation_only_guidance() -> None:
    guidance = actionable_official_web_failure(
        question="리바로 급여기준 알려줘",
        source_domain="hira",
        reason_code="IDENTITY_MISMATCH",
        provider_outcome="not_called",
    )

    assert guidance is not None
    assert "제품 또는 성분 구성이 요청한 브랜드와 일치하지 않아" in guidance
    assert "검색어: 리바로" in guidance
    assert "정확한 제품명 또는 성분 구성을 확인해 다시 요청" in guidance
    assert "웹 보완 자료" not in guidance


def test_unavailable_web_interface_returns_registered_entry_point() -> None:
    guidance = actionable_official_web_failure(
        question="아일리아 허가사항 알려줘",
        source_domain="regulatory",
        reason_code="UPSTREAM_UNAVAILABLE",
        provider_outcome="unavailable",
    )

    assert guidance is not None
    assert "현재 연결된 조회 도구에서는 요청 항목을 직접 제공하지 않습니다." in guidance
    assert "[식품의약품안전처 의약품 검색](https://nedrug.mfds.go.kr/" in guidance
    assert "검색어: 아일리아 허가사항" in guidance


@pytest.mark.parametrize(
    ("internal_only", "authoritative_nonexistence_proven"),
    ((True, False), (False, True)),
)
def test_actionable_guidance_never_bypasses_existing_exclusions(
    internal_only: bool,
    authoritative_nonexistence_proven: bool,
) -> None:
    assert (
        actionable_official_web_failure(
            question="아일리아 급여기준 알려줘",
            source_domain="hira",
            reason_code="NO_RECORD_FOUND",
            provider_outcome="empty",
            internal_only=internal_only,
            authoritative_nonexistence_proven=authoritative_nonexistence_proven,
        )
        is None
    )


@pytest.mark.parametrize(
    ("error_code", "expected"),
    (
        ("NO_EVIDENCE", "empty"),
        ("NO_DATA", "empty"),
        ("TOOL_TIMEOUT", "timeout"),
        ("UPSTREAM_UNAVAILABLE", "error"),
    ),
)
def test_web_provider_outcome_preserves_zero_timeout_and_error(
    error_code: str,
    expected: str,
) -> None:
    result = AgentResult(
        status="error",
        answer="",
        tool_calls=(
            {
                "tool": "web_search",
                "source": "web_search",
                "status": "error",
                "render_data": {"error_code": error_code, "evidence": []},
            },
        ),
        sources=(),
        traces=(),
        fallback_code=None,
    )

    assert _web_provider_outcome(result) == expected
