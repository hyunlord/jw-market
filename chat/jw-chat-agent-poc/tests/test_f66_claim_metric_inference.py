from __future__ import annotations

import json
from pathlib import Path

from jw_chat_agent_poc.orchestrator.market_insights import render_market_narrative
from jw_chat_agent_poc.orchestrator.provenance import evidence_from_calls
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings
from jw_chat_agent_poc.service.evidence_binding_rules import claim_metrics_for_token


_FIXTURE = Path(__file__).parent / "fixtures" / "f66_live_rc1_capture.json"


def _live_turn() -> dict:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert payload["capture_kind"] == "live_runtime_direct_pipeline"
    assert payload["synthetic_fixture"] is False
    return payload["scenario"]["turns"][0]


def _live_replay() -> tuple[str, str, tuple]:
    turn = _live_turn()
    narrative = render_market_narrative(turn["tool_calls"])
    answer = f"{narrative}\n\n{turn['pre_binding_answer']}"
    facts = evidence_from_calls(turn["tool_calls"], turn["pre_binding_answer"])
    return turn["question"], answer, facts


def test_live_capture_records_sales_token_as_market_share_mismatch() -> None:
    rejection = next(
        item
        for item in _live_turn()["claims_rejections"]
        if item["token"] == "80.39억원"
    )

    assert rejection["reason"] == "METRIC_MISMATCH"
    assert rejection["expected"]["metric"] == ["시장점유율"]
    assert {candidate["metric"] for candidate in rejection["candidates"]} == {"매출"}
    assert {
        axis
        for candidate in rejection["candidates"]
        for axis in candidate["mismatched_axes"]
    } == {"metric"}


def test_live_sales_claim_uses_sales_context_without_breaking_competitors() -> None:
    question, answer, facts = _live_replay()

    assert claim_metrics_for_token(answer, "80.39억원") == ("매출",)
    assert claim_metrics_for_token(answer, "3.76%") == ("시장점유율",)
    gate = verify_claim_bindings(question=question, answer=answer, facts=facts)

    assert "80.39억원" not in gate.blocked_numbers
    assert "195.24억원" not in gate.blocked_numbers


def test_live_sales_claim_keeps_unbound_derived_numbers_fail_closed() -> None:
    question, answer, facts = _live_replay()

    gate = verify_claim_bindings(
        question=question,
        answer=answer,
        facts=facts,
    )

    assert "0.17%p" in gate.blocked_numbers
    assert "0.76억원" in gate.blocked_numbers
