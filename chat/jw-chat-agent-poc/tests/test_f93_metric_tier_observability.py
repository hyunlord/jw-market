from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.service.evidence_binding import BindingVerification
from jw_chat_agent_poc.service.evidence_binding_observability import (
    _without_table_lines,
    binding_pipeline_observability,
)
from jw_chat_agent_poc.service.evidence_binding_rules import claim_metrics_for_token


QUESTION = "리바로 매출 알려줘"


def _gate(*blocked_numbers: str) -> BindingVerification:
    return BindingVerification(
        answer="filtered",
        status="fail" if blocked_numbers else "pass",
        disposition="partial" if blocked_numbers else "answered",
        blocked_claim_count=len(blocked_numbers),
        blocked_reasons=("METRIC_MISMATCH",) if blocked_numbers else (),
        blocked_numbers=blocked_numbers,
    )


def _occurrences(answer: str, *blocked: str, question: str = QUESTION) -> list[dict]:
    pipeline = binding_pipeline_observability(
        question=question,
        answer=answer,
        facts=(),
        expected_entities=("리바로",),
        expected_market_ids=frozenset(),
        gate=_gate(*blocked),
        fact_input={
            "source": "reconstructed_from_tool_calls",
            "input_item_count": 0,
            "loaded_fact_count": 0,
            "discarded_count": 0,
            "discard_reason": "",
        },
    )
    return list(pipeline["occurrences"])


def _tier_for(answer: str, token: str, *, question: str = QUESTION) -> str:
    for occurrence in _occurrences(answer, token, question=question):
        if occurrence["token_length"] == len(token):
            return occurrence["metric_inference"]["tier"]
    raise AssertionError(f"no occurrence recorded for {token!r}")


TABLE = "| 기간 | 매출 | MS |\n| --- | --- | --- |\n| 2025-08 | 90.86억원 | 3.93% |\n"


def test_tier_segment_when_a_sentence_supplies_the_metric() -> None:
    assert _tier_for("점유율은 90.86억원 입니다.\n", "90.86억원") == "segment"


def test_tier_table_header_for_a_value_column() -> None:
    assert _tier_for(TABLE, "90.86억원") == "table_header"


def test_tier_table_header_for_a_period_column() -> None:
    assert _tier_for(TABLE, "2025-08") == "table_header"


def test_tier_segment_wins_even_when_both_sources_agree() -> None:
    # The first discriminator attempted in F92 reported "ambiguous" here.
    answer = "매출은 90.86억원 입니다.\n| 기간 | 매출 |\n| --- | --- |\n| 2025-08 | 90.86억원 |\n"
    assert _tier_for(answer, "90.86억원") == "segment"


def test_tier_segment_when_the_two_sources_disagree() -> None:
    answer = "점유율 변동 90.86억원 입니다.\n| 기간 | 매출 |\n| --- | --- |\n| 2025-08 | 90.86억원 |\n"
    assert _tier_for(answer, "90.86억원") == "segment"


def test_tier_question_fallback_when_neither_segment_nor_table_matches() -> None:
    assert _tier_for("값은 90.86억원 입니다\n", "90.86억원") == "question_fallback"


def test_tier_none_when_no_source_supplies_a_metric() -> None:
    # A question carrying no metric term leaves the fallback empty too.
    assert _tier_for("값은 90.86억원 입니다\n", "90.86억원", question="안녕?") == "none"


def test_source_count_reports_the_cross_table_union_width() -> None:
    answer = (
        "| 기간 | 매출 |\n| --- | --- |\n| 2025-08 | 90.86억원 |\n\n"
        "| 구분 | MS |\n| --- | --- |\n| x | 90.86억원 |\n"
    )
    occurrence = next(
        item
        for item in _occurrences(answer, "90.86억원")
        if item["token_length"] == len("90.86억원")
    )
    assert occurrence["metric_inference"]["tier"] == "table_header"
    assert occurrence["metric_inference"]["source_count"] == 2
    assert list(occurrence["expected"]["metric"]) == ["매출", "시장점유율"]


def test_claim_text_length_is_reported() -> None:
    occurrence = next(
        item
        for item in _occurrences(TABLE, "90.86억원")
        if item["token_length"] == len("90.86억원")
    )
    length = occurrence["metric_inference"]["claim_text_length"]
    assert isinstance(length, int)
    assert length > 0


def test_decision_path_agreement_is_reported_once_per_response() -> None:
    pipeline = binding_pipeline_observability(
        question=QUESTION,
        answer=TABLE,
        facts=(),
        expected_entities=("리바로",),
        expected_market_ids=frozenset(),
        gate=_gate("90.86억원"),
        fact_input={
            "source": "reconstructed_from_tool_calls",
            "input_item_count": 0,
            "loaded_fact_count": 0,
            "discarded_count": 0,
            "discard_reason": "",
        },
    )
    assert pipeline["decision_path_agrees"] is True


def test_metric_inference_carries_no_free_text() -> None:
    """The block must be enum + int + int only - no answer fragment may leak."""
    answer = "리바로 점유율은 90.86억원 입니다.\n" + TABLE
    for occurrence in _occurrences(answer, "90.86억원"):
        block = occurrence["metric_inference"]
        assert set(block) == {"tier", "source_count", "claim_text_length"}
        assert block["tier"] in {
            "segment",
            "table_header",
            "question_fallback",
            "none",
        }
        assert isinstance(block["source_count"], int)
        assert isinstance(block["claim_text_length"], int)
        serialized = json.dumps(block, ensure_ascii=False)
        for fragment in ("리바로", "점유율", "90.86", "억원", "MS", "기간"):
            assert fragment not in serialized


def test_blocked_free_response_still_reports_a_well_formed_tier() -> None:
    """No blocked token must not be confused with instrumentation failure."""
    occurrences = _occurrences(TABLE)
    assert occurrences
    for occurrence in occurrences:
        assert occurrence["decision"] == "pass"
        assert "metric_inference" in occurrence
        assert occurrence["metric_inference"]["tier"] in {
            "segment",
            "table_header",
            "question_fallback",
            "none",
        }


@pytest.mark.parametrize(
    "text",
    [
        "a\n| x |\n",
        "a\n| x |",
        "a\n\n| x |\n\nb\n",
        "| x |\n| y |\n",
        "",
    ],
)
def test_table_blanking_preserves_newline_structure(text: str) -> None:
    """Blanking, never deleting: a deleted line would merge prose segments and
    change what the segment branch answers, which is the discriminator's premise."""
    stripped = _without_table_lines(text)
    assert stripped.count("\n") == text.count("\n")
    assert len(stripped.split("\n")) == len(text.split("\n"))


def test_table_blanking_keeps_the_segment_answer_but_drops_the_table_answer() -> None:
    # The assertion is on the segment answer being PRESERVED rather than on a
    # literal metric. The literal used to be ("시장점유율",) for a 억원 amount,
    # which was the RC1 defect itself; what this test protects is the
    # discriminator's premise, and that premise is unchanged.
    answer = "점유율은 90.86억원 입니다.\n" + TABLE
    with_tables = claim_metrics_for_token(answer, "90.86억원")
    assert with_tables, "the segment branch must answer for this input"
    assert claim_metrics_for_token(_without_table_lines(answer), "90.86억원") == with_tables
    # and the table-only answer must disappear once blanked
    assert claim_metrics_for_token(_without_table_lines(TABLE), "90.86억원") == ()


def test_decision_path_disagreement_is_detected() -> None:
    """A token the gate blocked but the observed text does not contain means the
    two paths saw different answers; the tier readings must not be trusted."""
    diverged = binding_pipeline_observability(
        question=QUESTION,
        answer=TABLE,
        facts=(),
        expected_entities=("리바로",),
        expected_market_ids=frozenset(),
        gate=_gate("11111.11억원"),
        fact_input={
            "source": "reconstructed_from_tool_calls",
            "input_item_count": 0,
            "loaded_fact_count": 0,
            "discarded_count": 0,
            "discard_reason": "",
        },
    )
    assert diverged["decision_path_agrees"] is False
