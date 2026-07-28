"""BND — public projection of binding_decision.

An internal dict is not evidence. W (selection_trace), A-0 (metric_inference)
and O (reason_code) each verified an internal structure, went green, and were
absent from the live response. These tests walk the real projection path:

    _apply_evidence_binding_gate (app.py)  ->  result["_qa_claim_gate"]
    trace_envelope (runtime_provenance.py) ->  trace["qa_trace"]["claims"]

and assert the field is present in the ENVELOPE that ships to the caller.
"""
from __future__ import annotations

import json

from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope

QUESTION = "리바로 2024-01 매출 알려줘"


_EVIDENCE = [
    {
        "fact_id": "f1",
        "label": "리바로 매출",
        "value": "10.5",
        "source": "UBIST",
        "tool": "get_brand_metric",
        "path": "mart",
        "period": "2024-01",
        "allowed_numbers": ("10.5억원", "10.5"),
        "entity": "리바로",
        "metric": "매출",
        "unit": "억원",
        "source_grade": "AUTHORITATIVE",
    }
]

# expected_entities_from_result reads the routing_v4 proposal, not the question,
# for brand entities. Without this the binder has no expected entity and every
# token short-circuits to clean_pass -- which would make this test vacuous.
_ROUTER_DIAGNOSTICS = {
    "routing_v4": {
        "proposed_routing_signature": {
            "proposed_calls": [
                {"normalized_args": {"brand": "리바로"}},
            ]
        }
    }
}


def _result(answer: str) -> dict:
    return {
        "context_scope": "MARKET",
        "markdown_response": {
            "fact_md": "| 리바로 | 매출 | 2024-01 | 10.5억원 |",
            "evidence": _EVIDENCE,
        },
        "router_diagnostics": _ROUTER_DIAGNOSTICS,
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "status": "ok",
                "render_data": {"brand": "리바로", "value": "10.5"},
            }
        ],
        "answer": answer,
    }


def _envelope(answer: str) -> dict:
    result = _result(answer)
    gated = _apply_evidence_binding_gate(QUESTION, answer, result)
    return trace_envelope(
        question=QUESTION,
        result=result,
        answer=gated,
        charts=[],
        timing={},
        conversation_id="bnd-test",
    )


def _claims(envelope: dict) -> dict:
    return envelope["qa_trace"]["claims"]


def test_binding_decision_reaches_the_public_envelope() -> None:
    envelope = _envelope("리바로 매출은 99.9억원입니다.")
    claims = _claims(envelope)

    assert "binding_decision" in claims, (
        "binding_decision was dropped between _qa_claim_gate and the envelope"
    )
    decision = claims["binding_decision"]
    assert set(decision) == {
        "decision_site",
        "substitution_triggered",
        "bind_attempted_count",
        "bind_succeeded_count",
        "blocked_reason_histogram",
    }

    # A key that projects only default values proves nothing. This fixture must
    # actually reach the all-or-nothing substitution site, so the projection is
    # shown carrying a NON-default verdict.
    assert decision["decision_site"] == "blocked_substitution"
    assert decision["substitution_triggered"] is True
    assert decision["bind_succeeded_count"] == 0
    assert decision["blocked_reason_histogram"] == [["MISSING_EVIDENCE_BINDING", 1]]


def test_envelope_is_json_serializable() -> None:
    """The envelope is serialized to the caller. Tuples/None must survive."""
    envelope = _envelope("리바로 매출은 99.9억원입니다.")
    payload = json.dumps(envelope, ensure_ascii=False, default=str)
    reloaded = json.loads(payload)
    decision = reloaded["qa_trace"]["claims"]["binding_decision"]
    assert decision["decision_site"]
    assert isinstance(decision["substitution_triggered"], bool)


def test_binding_decision_present_even_when_nothing_blocked() -> None:
    """Absence of blocking must still be reported, not silently omitted."""
    envelope = _envelope("리바로 매출은 10.5억원입니다.")
    decision = _claims(envelope)["binding_decision"]

    assert decision["substitution_triggered"] is False
    # explicit null, not a missing key
    assert "blocked_reason_histogram" in decision
    assert decision["blocked_reason_histogram"] is None


def test_public_projection_carries_no_token_values_or_prose() -> None:
    """enum / int / bool only. No token strings, no answer fragments."""
    envelope = _envelope("리바로 매출은 99.9억원입니다.")
    decision = _claims(envelope)["binding_decision"]
    serialized = json.dumps(decision, ensure_ascii=False)

    # the blocked token value must not appear in the new field
    assert "99.9" not in serialized
    # no answer prose
    assert "리바로" not in serialized
    assert "매출" not in serialized

    for key, value in decision.items():
        if key == "blocked_reason_histogram" and value is not None:
            for reason, count in value:
                # reasons are SCREAMING_SNAKE enums, counts are ints
                assert reason.replace("_", "").isalnum() and reason.isupper()
                assert isinstance(count, int)
        else:
            assert value is None or isinstance(value, (bool, int, str))
