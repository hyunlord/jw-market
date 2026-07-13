from __future__ import annotations

from pipeline.scripts.agent3.run_source import (
    _classify_workflow_call,
    evaluate_idempotency_gate,
)
from pipeline.scripts.agent3.source_loader import ExistingAgent3SourceState


def _old(rev: int) -> ExistingAgent3SourceState:
    return ExistingAgent3SourceState(
        input_hash="stored-hash",
        workflow_rev=rev,
        profile_json={"brand": "A"},
        strength_candidates_json=[],
        strength_summary_json={"strength_items": []},
    )


def test_classify_new_unit_when_no_prior_row() -> None:
    assert _classify_workflow_call(None, 5692, content_matches=False) == "calls_new"


def test_classify_revision_change_takes_precedence_over_content() -> None:
    assert _classify_workflow_call(_old(5365), 5692, content_matches=True) == "calls_revision_change"
    assert _classify_workflow_call(_old(5656), 5692, content_matches=False) == "calls_revision_change"


def test_classify_input_change_when_same_rev_and_content_differs() -> None:
    assert _classify_workflow_call(_old(5692), 5692, content_matches=False) == "calls_input_change"


def test_classify_unexplained_when_same_rev_and_content_matches() -> None:
    # the wasteful stale-hash re-call: same rev, identical canonical content
    assert _classify_workflow_call(_old(5692), 5692, content_matches=True) == "calls_unexplained"


def test_gate_green_only_when_no_unexplained_calls() -> None:
    green = evaluate_idempotency_gate(
        {
            "workflow_calls": 12,
            "calls_new": 4,
            "calls_revision_change": 8,
            "calls_input_change": 0,
            "calls_unexplained": 0,
        }
    )
    assert green["status"] == "green"
    assert green["calls_new"] == 4
    assert green["calls_revision_change"] == 8


def test_gate_red_only_when_unexplained_calls_present() -> None:
    red = evaluate_idempotency_gate(
        {
            "workflow_calls": 5,
            "calls_new": 0,
            "calls_revision_change": 0,
            "calls_input_change": 0,
            "calls_unexplained": 5,
        }
    )
    assert red["status"] == "red"
    assert red["calls_unexplained"] == 5


def test_gate_ignores_legitimate_call_reasons() -> None:
    result = evaluate_idempotency_gate(
        {
            "workflow_calls": 30,
            "calls_new": 10,
            "calls_revision_change": 10,
            "calls_input_change": 10,
            "calls_unexplained": 0,
        }
    )
    assert result["status"] == "green"


def test_gate_defaults_missing_counters_to_zero() -> None:
    result = evaluate_idempotency_gate({})
    assert result["status"] == "green"
    assert result["calls_unexplained"] == 0
