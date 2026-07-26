from __future__ import annotations

from dataclasses import asdict
import json

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate
from jw_chat_agent_poc.service.evidence_binding_rules import without_bound_identifiers
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


def _fact(
    *,
    fact_id: str,
    metric: str,
    value: str = "80.39억원",
    entity: str = "리바로",
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        label=metric,
        value=value,
        source="UBIST",
        tool="get_brand_metric",
        path=f"render_data.{fact_id}",
        period="2026-05",
        allowed_numbers=(value,),
        entity=entity,
        metric=metric,
        unit="억원",
        source_grade="AUTHORITATIVE",
        view="general_view",
        market_id="566",
    )


def _result(*facts: EvidenceFact) -> dict[str, object]:
    return {
        "general_view_ready": True,
        "resolution": {"market_id": "566"},
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
        "markdown_response": {"evidence": [asdict(fact) for fact in facts]},
        "tool_calls": [],
    }


def test_repeated_numeric_claims_are_traced_as_distinct_occurrences() -> None:
    answer = (
        "## 대표 지표\n\n"
        "리바로 매출은 80.39억원입니다.\n\n"
        "| 기간 | 매출 |\n"
        "| --- | --- |\n"
        "| 2026-05 | 80.39억원 |\n\n"
        "| 순위 | 브랜드 | 매출 |\n"
        "| --- | --- | --- |\n"
        "| 6 | 리바로 | 80.39억원 |"
    )
    result = _result(
        _fact(fact_id="sales", metric="매출"),
        _fact(fact_id="market-size", metric="시장규모"),
    )

    _apply_evidence_binding_gate("일반뷰 리바로 매출은?", answer, result)

    trace = result["_qa_claim_gate"]["pipeline_observability"]
    repeated = [
        item
        for item in trace["occurrences"]
        if item["unit"] == "억원"
    ]
    assert len(repeated) == 3
    assert len({item["occurrence_id"] for item in repeated}) == 3
    assert len({tuple(item["char_range"]) for item in repeated}) == 3
    assert {item["location"]["kind"] for item in repeated} == {"prose", "table"}
    assert all(item["decision"] == "pass" for item in repeated)
    assert all(item["decision_scope"] == "unique_token_string" for item in repeated)
    assert trace["pipeline"]["decision_unit"] == "unique_token_string"
    assert "80.39" not in json.dumps(trace, ensure_ascii=False)


def test_stage_inventory_distinguishes_generation_from_later_filtering() -> None:
    result = _result(
        _fact(fact_id="sales", metric="매출"),
        _fact(fact_id="market-size", metric="시장규모"),
    )

    _apply_evidence_binding_gate(
        "일반뷰 리바로 매출은?",
        "리바로 매출은 80.39억원입니다.",
        result,
    )

    trace = result["_qa_claim_gate"]["pipeline_observability"]
    occurrence = next(item for item in trace["occurrences"] if item["unit"] == "억원")
    stage_counts = {
        stage["stage"]: stage["fact_count"]
        for stage in occurrence["stages"]
    }
    assert stage_counts == {
        "facts_loaded": 2,
        "binding_metadata_eligible": 2,
        "value_candidates": 2,
        "axis_compatible": 1,
        "source_grade_usable": 1,
        "operand_usable": 1,
    }
    axis_stage = next(
        stage for stage in occurrence["stages"] if stage["stage"] == "axis_compatible"
    )
    assert axis_stage["removed_count"] == 1
    assert axis_stage["removed"][0]["reason"] == "metric"
    assert occurrence["decision"] == "pass"
    assert trace["pipeline"]["fact_input"] == {
        "source": "serialized_markdown_evidence",
        "input_item_count": 2,
        "loaded_fact_count": 2,
        "discarded_count": 0,
        "discard_reason": "",
    }


def test_malformed_serialized_fact_is_counted_before_binding() -> None:
    result = _result(_fact(fact_id="sales", metric="매출"))
    result["markdown_response"]["evidence"].append({"fact_id": "malformed"})

    _apply_evidence_binding_gate(
        "일반뷰 리바로 매출은?",
        "리바로 매출은 80.39억원입니다.",
        result,
    )

    fact_input = result["_qa_claim_gate"]["pipeline_observability"]["pipeline"][
        "fact_input"
    ]
    assert fact_input == {
        "source": "serialized_markdown_evidence",
        "input_item_count": 2,
        "loaded_fact_count": 1,
        "discarded_count": 1,
        "discard_reason": "malformed_serialized_fact",
    }


def test_observability_reaches_runtime_trace_without_raw_values() -> None:
    answer = "리바로 매출은 80.39억원입니다."
    result = _result(_fact(fact_id="sales", metric="매출"))
    final_answer = _apply_evidence_binding_gate(
        "일반뷰 리바로 매출은?",
        answer,
        result,
    )

    trace = trace_envelope(
        question="일반뷰 리바로 매출은?",
        result=result,
        answer=final_answer,
        charts=(),
        timing={"stages": []},
        conversation_id="f55-runtime",
    )

    projected = trace["qa_trace"]["claims"]["pipeline_observability"]
    assert projected == result["_qa_claim_gate"]["pipeline_observability"]
    assert answer not in json.dumps(projected, ensure_ascii=False)
    assert "80.39" not in json.dumps(projected, ensure_ascii=False)


def test_binder_input_fingerprint_covers_every_blocked_token_without_raw_values() -> None:
    result = _result(_fact(fact_id="market-size", metric="시장규모"))
    answer = "리바로 매출은 80.39억원이고 증감은 0.76억원입니다."

    _apply_evidence_binding_gate("일반뷰 리바로 매출은?", answer, result)

    gate = result["_qa_claim_gate"]
    trace = gate["pipeline_observability"]
    binder_input = trace["binder_input"]
    claim_text = without_bound_identifiers(answer, {"리바로"})
    assert binder_input["basis"] == "claim_text_after_expected_identifier_removal"
    assert binder_input["chars"] == len(claim_text)
    assert binder_input["utf8_bytes"] == len(claim_text.encode())
    assert len(binder_input["sha256"]) == 64
    assert binder_input["source_answer"]["chars"] == len(answer)
    assert binder_input["source_answer"]["utf8_bytes"] == len(answer.encode())
    assert binder_input["truncated"] is False
    assert binder_input["blocked_token_count"] == len(gate["blocked_numbers"])
    assert binder_input["blocked_tokens_covered"] is True
    assert all(item["occurrence_ids"] for item in binder_input["blocked_token_refs"])
    serialized = json.dumps(trace, ensure_ascii=False)
    assert answer not in serialized
    assert "80.39" not in serialized
    assert "0.76" not in serialized
    assert "render_data" not in serialized


def test_observability_is_bounded_under_large_fact_and_occurrence_population() -> None:
    facts = tuple(
        _fact(
            fact_id=f"candidate-{index}",
            metric=f"시장규모-{index}",
            entity=f"브랜드-{index}",
        )
        for index in range(40)
    )
    answer = "\n".join(
        "| 리바로 | 80.39억원 |"
        for _index in range(40)
    )
    result = _result(*facts)

    _apply_evidence_binding_gate("일반뷰 리바로 매출은?", answer, result)

    trace = result["_qa_claim_gate"]["pipeline_observability"]
    assert trace["occurrence_count"] > len(trace["occurrences"])
    assert trace["occurrences_truncated"] is True
    assert len(trace["occurrences"]) <= 32
    assert trace["fact_inventory"]["metric_counts_truncated"] is True
    assert trace["fact_inventory"]["axis_combinations_truncated"] is True
    blocked_ref = max(
        trace["binder_input"]["blocked_token_refs"],
        key=lambda item: item["occurrence_count"],
    )
    assert blocked_ref["occurrence_count"] == 40
    assert len(blocked_ref["occurrence_ids"]) == 8
    assert blocked_ref["occurrence_ids_truncated"] is True
    assert len(json.dumps(trace, ensure_ascii=False).encode()) < 64_000
