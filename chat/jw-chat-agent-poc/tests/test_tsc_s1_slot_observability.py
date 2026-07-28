from __future__ import annotations

import hashlib

from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.tools.metrics.market_scope_intent import (
    _explicit_view,
    _normalize,
    detect_market_scope_intent,
)


def _result(
    answer: str,
    *,
    source: str | None = None,
    period: str | None = None,
    metric: str | None = None,
    anaphora: dict[str, object] | None = None,
) -> dict[str, object]:
    render_data = {
        key: value
        for key, value in {
            "brand": "리바로",
            "period": period,
            "metric": metric,
            "query_spec": {"source": source} if source else None,
        }.items()
        if value is not None
    }
    call = {
        "tool": "get_brand_metric",
        "status": "ok",
        "render_data": render_data,
    }
    return {
        "conversation_fallback_ready": True,
        "answer": answer,
        "tool_calls": [call],
        "sources": [source.upper()] if source else [],
        "resolution": {"canonical_brand": "리바로"},
        "_qa_anaphora": anaphora
        or {
            "status": "not_anaphoric",
            "recogniser": None,
            "candidate_shape": False,
            "unresolved_reference": False,
        },
    }


def _slots(question: str, result: dict[str, object]) -> tuple[str, dict[str, object]]:
    final = compute_final_answer(question, result, "tsc-s1")
    return final.text, final.trace["qa_trace"]["routing"]["slots"]


def test_defaulted_market_view_is_not_reported_as_explicit() -> None:
    question = "고지혈증 시장 HHI"

    assert _explicit_view(_normalize(question)) is None
    assert detect_market_scope_intent(question).view_type == "market_landscape"

    answer, slots = _slots(question, _result("HHI 기준 답변입니다.", metric="hhi"))

    assert answer == "HHI 기준 답변입니다."
    assert slots["view"]["presence"] == "unset"
    assert slots["view"]["status"] == "default_suppressed"
    assert slots["metric"]["presence"] == "explicit"


def test_explicit_requested_source_records_mismatch_without_changing_answer() -> None:
    expected = "요청 소스와 실제 소스가 다른 기준 답변입니다."

    answer, slots = _slots(
        "리바로 IQVIA 매출 알려줘",
        _result(expected, source="ubist", metric="sales"),
    )

    assert answer == expected
    assert hashlib.sha256(answer.encode()).hexdigest() == hashlib.sha256(expected.encode()).hexdigest()
    assert slots["source"]["presence"] == "explicit"
    assert slots["source"]["comparison"] == "mismatch"
    assert slots["source"]["requested_present"] is True
    assert slots["source"]["served_present"] is True


def test_unspecified_source_and_view_remain_unset() -> None:
    answer, slots = _slots(
        "리바로 매출 알려줘",
        _result("미명시 기준 답변입니다.", source="ubist", metric="sales"),
    )

    assert answer == "미명시 기준 답변입니다."
    assert slots["source"]["presence"] == "unset"
    assert slots["metric"]["presence"] == "unset"
    assert slots["view"]["presence"] == "unset"


def test_unavailable_external_source_uses_registered_allow_list_value() -> None:
    _, slots = _slots(
        "리바로 Cortellis 매출 알려줘",
        _result("외부 소스 기준 답변입니다.", source="ubist", metric="sales"),
    )

    assert slots["source"]["presence"] == "explicit"
    assert slots["source"]["requested_value"] == "cortellis"
    assert slots["source"]["comparison"] == "mismatch"


def test_bare_period_followup_is_reported_as_inherited() -> None:
    answer, slots = _slots(
        "2024년은?",
        _result(
            "승계 기준 답변입니다.",
            source="ubist",
            period="2024",
            metric="sales",
            anaphora={
                "status": "resolved",
                "recogniser": "bare_period",
                "candidate_shape": True,
                "unresolved_reference": False,
            },
        ),
    )

    assert answer == "승계 기준 답변입니다."
    assert slots["period"]["presence"] == "inherited"


def test_explicit_general_view_uses_three_value_view_domain() -> None:
    _, slots = _slots(
        "고지혈증 일반뷰 HHI",
        _result(
            "일반뷰 기준 답변입니다.",
            metric="hhi",
            anaphora={
                "status": "resolved",
                "recogniser": "bare_market",
                "candidate_shape": True,
                "unresolved_reference": False,
            },
        ),
    )

    assert slots["view"]["presence"] == "explicit"
    assert slots["view"]["requested_value"] == "general_view"


def test_non_comparable_slots_report_extractor_absence() -> None:
    _, slots = _slots(
        "리바로 분기 매출",
        _result("분기 기준 답변입니다.", source="ubist", metric="sales"),
    )

    assert slots["granularity"]["status"] == "requested_slot_absent"
    assert slots["granularity"]["comparison"] == "not_applicable"
    assert slots["relation"]["comparison"] == "not_applicable"
    assert slots["scope"]["comparison"] == "not_applicable"


def test_relation_is_observed_without_becoming_a_comparison_slot() -> None:
    _, slots = _slots(
        "리바로 경쟁사 영업활동",
        _result("관계 기준 답변입니다.", source="ubist", metric="activity"),
    )

    assert slots["relation"]["presence"] == "explicit"
    assert slots["relation"]["status"] == "extracted"
    assert slots["relation"]["comparison"] == "not_applicable"


def test_same_question_produces_same_slot_observation() -> None:
    question = "리바로 IQVIA 매출 알려줘"

    first = _slots(question, _result("반복 기준 답변입니다.", source="ubist", metric="sales"))
    second = _slots(question, _result("반복 기준 답변입니다.", source="ubist", metric="sales"))

    assert first == second


def test_public_trace_contains_only_bounded_slot_metadata() -> None:
    _, slots = _slots(
        "리바로 IQVIA 매출 알려줘",
        _result("공개 투영 답변입니다.", source="ubist", metric="sales"),
    )

    assert set(slots) == {
        "entity",
        "source",
        "metric",
        "view",
        "period",
        "granularity",
        "relation",
        "scope",
    }
    assert "리바로" not in repr(slots)
    assert "IQVIA" not in repr(slots)
    assert "공개 투영 답변" not in repr(slots)
