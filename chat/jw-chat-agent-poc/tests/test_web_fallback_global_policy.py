from __future__ import annotations

import pytest

from jw_chat_agent_poc.tool_use.routing_v4_execution import (
    official_web_fallback_eligible,
    official_web_fallback_policy,
)
from jw_chat_agent_poc.tool_use.routing_v4_runtime import _explicit_internal_only


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
