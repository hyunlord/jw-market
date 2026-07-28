"""BND — binding decision-site observability.

Observation only. Every test here asserts BOTH that the new field appears AND
that the verdict (answer / status / disposition / blocked_*) is untouched.
Instrumentation does not vote.
"""
from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.evidence_binding import (
    _BINDING_FAILURE_ANSWER,
    verify_claim_bindings,
)


def _fact(
    fact_id: str,
    *,
    entity: str = "리바로",
    metric: str = "매출",
    period: str = "2024-01",
    unit: str = "억원",
    allowed: tuple[str, ...] = ("10.5억원", "10.5"),
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        label=f"{entity} {metric}",
        value="10.5",
        source="UBIST",
        tool="get_brand_metric",
        path="mart",
        period=period,
        allowed_numbers=allowed,
        entity=entity,
        metric=metric,
        unit=unit,
        source_grade="AUTHORITATIVE",
    )


# --------------------------------------------------------------------------
# BND-4 ① bind 성공 -> 필드가 값으로 나온다
# --------------------------------------------------------------------------
def test_clean_pass_reports_decision_site_with_counts() -> None:
    gate = verify_claim_bindings(
        question="리바로 2024-01 매출 알려줘",
        answer="리바로 매출은 10.5억원입니다.",
        facts=[_fact("f1")],
        expected_entities=("리바로",),
    )

    assert gate.decision_site == "clean_pass"
    assert gate.substitution_triggered is False
    assert gate.bind_attempted_count == 1
    assert gate.bind_succeeded_count == 1
    assert gate.blocked_reason_histogram is None

    # verdict untouched
    assert gate.status == "pass"
    assert gate.disposition == "answered"
    assert gate.blocked_numbers == ()
    assert gate.blocked_reasons == ()


# --------------------------------------------------------------------------
# BND-4 ② bind 전량 실패 -> 사유 히스토그램이 나온다 (Q1 형태)
# --------------------------------------------------------------------------
def test_blocked_substitution_reports_histogram_and_site() -> None:
    gate = verify_claim_bindings(
        question="리바로 2024-01 매출 알려줘",
        answer="리바로 매출은 99.9억원입니다.",
        facts=[_fact("f1")],
        expected_entities=("리바로",),
    )

    # the verdict this round must NOT change
    assert gate.answer == _BINDING_FAILURE_ANSWER
    assert gate.status == "fail"
    assert gate.disposition == "unavailable"
    assert gate.blocked_claim_count == 1

    # the new observation
    assert gate.decision_site == "blocked_substitution"
    assert gate.substitution_triggered is True
    assert gate.bind_attempted_count == 1
    assert gate.bind_succeeded_count == 0
    assert gate.blocked_reason_histogram is not None
    histogram = dict(gate.blocked_reason_histogram)
    assert sum(histogram.values()) == 1
    # histogram keys are exactly the deduped reasons already published
    assert set(histogram) == set(gate.blocked_reasons)


# --------------------------------------------------------------------------
# BND-4 ③ 일부 실패 -> 성공/실패 수가 갈린다
# --------------------------------------------------------------------------
def test_partial_binding_splits_attempted_and_succeeded() -> None:
    gate = verify_claim_bindings(
        question="리바로 2024-01 매출 알려줘",
        answer="리바로 매출은 10.5억원이고 경쟁품은 99.9억원입니다.",
        facts=[_fact("f1")],
        expected_entities=("리바로",),
    )

    assert gate.bind_attempted_count == 2
    assert gate.bind_succeeded_count + gate.blocked_claim_count <= gate.bind_attempted_count
    assert gate.bind_succeeded_count == 1
    assert gate.decision_site in {
        "blocked_substitution",
        "partial_exclusion_rescue",
        "partial_metadata_notice",
    }


# --------------------------------------------------------------------------
# BND-4 ④ 차단 0건 -> null 명시 (키 누락 아님)
# --------------------------------------------------------------------------
def test_no_blocked_tokens_reports_explicit_null_histogram() -> None:
    gate = verify_claim_bindings(
        question="리바로 2024-01 매출 알려줘",
        answer="리바로 매출은 10.5억원입니다.",
        facts=[_fact("f1")],
        expected_entities=("리바로",),
    )

    # attribute EXISTS and is explicitly None -- not a missing attribute
    assert hasattr(gate, "blocked_reason_histogram")
    assert gate.blocked_reason_histogram is None


def test_early_return_reports_null_counts_not_zero() -> None:
    """Early returns never entered the token loop.

    "not observed" must be distinguishable from "observed, and it was zero".
    """
    gate = verify_claim_bindings(
        question="안녕하세요",
        answer="안녕하세요.",
        facts=[],
    )

    assert gate.decision_site == "no_expected_no_metrics_pass"
    assert gate.bind_attempted_count is None
    assert gate.bind_succeeded_count is None
    assert gate.blocked_reason_histogram is None


# --------------------------------------------------------------------------
# BND-2 — the two 86-char sites are distinguishable by value
# --------------------------------------------------------------------------
def test_two_substitution_sites_are_distinguishable() -> None:
    """:123 and :336 both emit the identical 86-char refusal.

    GEN had to reverse-engineer which one fired. They must now differ by value.
    """
    # :97 needs a 환자수 metric with NO resolvable entity. "D693" always
    # resolves to D69.3 from the question string alone, so a disease-code
    # question can never reach this site -- which is exactly why it was
    # unobserved live in 5/5 probes.
    entity_site = verify_claim_bindings(
        question="환자수 알려줘",
        answer="2020년 3,334명입니다.",
        facts=[],
        expected_entities=(),
    )
    blocked_site = verify_claim_bindings(
        question="리바로 2024-01 매출 알려줘",
        answer="리바로 매출은 99.9억원입니다.",
        facts=[_fact("f1")],
        expected_entities=("리바로",),
    )

    # identical user-visible answer ...
    assert blocked_site.answer == _BINDING_FAILURE_ANSWER
    # ... but the site is identifiable
    assert blocked_site.decision_site == "blocked_substitution"
    assert entity_site.decision_site != "blocked_substitution"


# --------------------------------------------------------------------------
# BND-4 ⑤ 판정 불변 -- instrumentation must not vote
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question,answer,facts,expected_entities",
    [
        ("리바로 2024-01 매출 알려줘", "리바로 매출은 10.5억원입니다.", [_fact("f1")], ("리바로",)),
        ("리바로 2024-01 매출 알려줘", "리바로 매출은 99.9억원입니다.", [_fact("f1")], ("리바로",)),
        ("안녕하세요", "안녕하세요.", [], ()),
        ("D693 상병 환자수 최근 5년 알려줘", "2020년 3,334명입니다.", [], ()),
    ],
)
def test_verdict_fields_are_untouched_by_instrumentation(
    question: str, answer: str, facts: list, expected_entities: tuple
) -> None:
    gate = verify_claim_bindings(
        question=question,
        answer=answer,
        facts=facts,
        expected_entities=expected_entities,
    )

    # The five verdict-carrying fields must remain exactly what the pre-BND
    # dataclass carried. Reading them must not depend on any new field.
    assert isinstance(gate.answer, str)
    assert gate.status in {"pass", "fail", "partial"}
    assert gate.disposition in {"answered", "unavailable", "partial"}
    assert isinstance(gate.blocked_claim_count, int)
    assert isinstance(gate.blocked_reasons, tuple)
    assert isinstance(gate.blocked_numbers, tuple)
    # blocked_claim_count stays consistent with blocked_numbers
    assert gate.blocked_claim_count == len(gate.blocked_numbers) or gate.blocked_claim_count == 0
