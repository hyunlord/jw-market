from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings
from jw_chat_agent_poc.service.evidence_binding_rules import (
    entity_matches,
    metric_matches,
    period_matches,
    scope_matches,
    unit_matches,
)


_LEGACY_BEHAVIOR_SHA256 = "39f2d450bad9e803a476cd308b6dbc86ca38311cb927092e21ca466477861419"


def _fact(
    *,
    fact_id: str = "market-size-mislabeled-sales",
    metric: str = "시장규모",
    period: str = "2026-05",
    unit: str = "억원",
    grade: str = "AUTHORITATIVE",
    market_id: str = "566",
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        label=metric,
        value="80.39억원",
        source="UBIST",
        tool="get_brand_metric",
        path="render_data.brand_value_series_10pt[2]",
        period=period,
        allowed_numbers=("80.39억원",),
        entity="리바로",
        metric=metric,
        unit=unit,
        source_grade=grade,
        view="general_view",
        market_id=market_id,
    )


def test_rejected_claim_exposes_bounded_axis_diagnostics_in_qa_trace() -> None:
    # Given: a live-shaped fact whose value matches but metric does not.
    evidence = _fact()
    result = {
        "general_view_ready": True,
        "answer": "리바로 매출은 80.39억원입니다.",
        "resolution": {"market_id": "566"},
        "markdown_response": {"evidence": [asdict(evidence)]},
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [
                        {
                            "normalized_args": {
                                "brand": "리바로",
                                "market_id": "566",
                            }
                        }
                    ]
                }
            }
        },
        "sources": ["UBIST"],
    }

    # When: the complete final-answer path applies evidence binding.
    final = compute_final_answer(
        "일반뷰 리바로 매출은?",
        result,
        "f51-rejection-diagnostics",
    )

    # Then: qa_trace pairs the token with its reason and six-axis comparison.
    assert final.trace["qa_trace"]["claims"]["rejections"] == (
        {
            "token": "80.39억원",
            "reason": "METRIC_MISMATCH",
            "expected": {
                "entity": ("566", "리바로"),
                "metric": ("매출",),
                "period": (),
                "unit": "억원",
                "view": ("general_view",),
                "market_id": ("566",),
            },
            "candidates": (
                {
                    "entity": "리바로",
                    "metric": "시장규모",
                    "period": "2026-05",
                    "unit": "억원",
                    "view": "general_view",
                    "market_id": "566",
                    "mismatched_axes": ("metric",),
                },
            ),
            "candidate_count": 1,
            "candidates_truncated": False,
        },
    )


def test_rejection_candidates_are_capped_and_do_not_expose_fact_payloads() -> None:
    # Given: more matching-value candidates than the diagnostic trace cap.
    facts = tuple(
            EvidenceFact(
                **{
                    **asdict(_fact(fact_id=f"candidate-{index}")),
                    "source": "credential-sentinel-must-not-leak",
                    "path": f"raw_text.full[{index}]",
                    "value": "confidential-value",
                }
        )
        for index in range(12)
    )

    # When: all candidates fail on metric.
    gate = verify_claim_bindings(
        question="일반뷰 리바로 매출은?",
        answer="리바로 매출은 80.39억원입니다.",
        facts=facts,
        expected_entities=("리바로",),
        expected_market_ids=frozenset({"566"}),
    )
    trace = gate.rejections[0].to_trace()
    serialized = json.dumps(trace, ensure_ascii=False)

    # Then: only metadata axes are bounded and truncation is explicit.
    assert trace["candidate_count"] == 12
    assert len(trace["candidates"]) == 8
    assert trace["candidates_truncated"] is True
    assert len(serialized.encode()) < 4_096
    assert "credential-sentinel-must-not-leak" not in serialized
    assert "raw_text.full" not in serialized
    assert "confidential-value" not in serialized
    assert "fact_id" not in serialized


def test_missing_evidence_records_zero_candidates_explicitly() -> None:
    # Given: a numeric claim with no evidence facts.
    gate = verify_claim_bindings(
        question="리바로 매출은?",
        answer="리바로 매출은 999.99억원입니다.",
        facts=(),
        expected_entities=("리바로",),
    )

    # When: the missing-evidence rejection is projected.
    trace = gate.rejections[0].to_trace()

    # Then: zero candidates is explicit rather than omitted.
    assert trace["reason"] == "MISSING_EVIDENCE_BINDING"
    assert trace["candidate_count"] == 0
    assert trace["candidates"] == ()
    assert trace["candidates_truncated"] is False


def test_scope_rejection_identifies_market_id_axis_without_exposing_raw_scope() -> None:
    # Given: all public claim axes match, but the fact belongs to another market.
    gate = verify_claim_bindings(
        question="일반뷰 리바로 매출은?",
        answer="리바로 매출은 80.39억원입니다.",
        facts=(_fact(metric="매출", market_id="555"),),
        expected_entities=("리바로",),
        expected_market_ids=frozenset({"566"}),
    )

    # When/Then: the existing scope matcher still rejects and diagnostics name
    # the internal axis without rendering an opaque scope signature.
    trace = gate.rejections[0].to_trace()
    assert trace["reason"] == "SCOPE_MISMATCH"
    assert trace["candidates"][0]["mismatched_axes"] == ("market_id",)
    assert "general_view:555" not in json.dumps(trace, ensure_ascii=False)


def test_binding_legacy_behavior_is_byte_identical_to_pre_f51_corpus() -> None:
    # Given: the fixed pre-F51 pass/fail/partial corpus.
    cases = {
        "pass": verify_claim_bindings(
            question="일반뷰 리바로 2026-05 매출은?",
            answer="리바로 2026-05 매출은 80.39억원입니다.",
            facts=(_fact(metric="매출"),),
            expected_entities=("리바로",),
            expected_market_ids=frozenset({"566"}),
        ),
        "metric_mismatch": verify_claim_bindings(
            question="일반뷰 리바로 2026-05 매출은?",
            answer="리바로 2026-05 매출은 80.39억원입니다.",
            facts=(_fact(),),
            expected_entities=("리바로",),
            expected_market_ids=frozenset({"566"}),
        ),
        "missing_evidence": verify_claim_bindings(
            question="리바로 매출은?",
            answer="리바로 매출은 999.99억원입니다.",
            facts=(),
            expected_entities=("리바로",),
        ),
        "incomplete_period": verify_claim_bindings(
            question="리바로 2026-05 매출은?",
            answer="리바로 2026-05 매출은 80.39억원입니다.",
            facts=(_fact(metric="매출", period=""),),
            expected_entities=("리바로",),
        ),
        "unverified_source": verify_claim_bindings(
            question="리바로 2026-05 매출은?",
            answer="리바로 2026-05 매출은 80.39억원입니다.",
            facts=(_fact(metric="매출", grade="UNVERIFIED"),),
            expected_entities=("리바로",),
        ),
    }
    legacy_keys = {
        "answer",
        "status",
        "disposition",
        "blocked_claim_count",
        "blocked_reasons",
        "blocked_numbers",
        "failure_kind",
    }

    # When: only the pre-existing BindingVerification fields are serialized.
    payload = {
        name: {
            key: value
            for key, value in asdict(result).items()
            if key in legacy_keys
        }
        for name, result in cases.items()
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()

    # Then: the bytes match the snapshot captured before implementation.
    assert hashlib.sha256(encoded).hexdigest() == _LEGACY_BEHAVIOR_SHA256


def test_matching_axis_functions_retain_pre_f51_results() -> None:
    # Given: one fully matching fact and one foreign-scope fact.
    matching = _fact(metric="매출")
    foreign_scope = _fact(metric="매출", market_id="555")

    # When/Then: each existing matcher keeps its pre-F51 decision.
    assert entity_matches(matching, {"리바로"}) is True
    assert entity_matches(matching, {"로수젯"}) is False
    assert metric_matches(matching, ("매출",)) is True
    assert metric_matches(matching, ("시장규모",)) is False
    assert period_matches(matching, ("2026-05",)) is True
    assert period_matches(matching, ("2026-04",)) is False
    assert unit_matches(matching, "80.39억원") is True
    assert unit_matches(matching, "80.39%") is False
    assert scope_matches(
        matching,
        frozenset({"general_view"}),
        frozenset({"566"}),
    ) is True
    assert scope_matches(
        foreign_scope,
        frozenset({"general_view"}),
        frozenset({"566"}),
    ) is False
