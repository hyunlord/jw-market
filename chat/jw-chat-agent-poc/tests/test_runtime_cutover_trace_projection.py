from __future__ import annotations

import hashlib
from typing import Any

import pytest

from jw_chat_agent_poc.service import runtime_provenance
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


def _trace(cutover: dict[str, str] | None) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"mode": "tool_use_agent"}
    if cutover is not None:
        diagnostics["canonical_router_cutover"] = cutover
    return trace_envelope(
        question="NCT01234567 임상시험 상세",
        result={
            "router_diagnostics": diagnostics,
            "tool_calls": [],
            "markdown_response": {"fact_md": "", "data_md": ""},
        },
        answer="관측 투영과 무관한 기존 답변",
        charts=(),
        timing={"stages": []},
        conversation_id="cutover-trace-test",
    )


@pytest.mark.parametrize(
    ("mode", "is_deterministic"),
    (("deterministic", True), ("agentic", False)),
)
def test_trace_projects_canonical_cutover_ownership(mode: str, is_deterministic: bool) -> None:
    trace = _trace(
        {
            "domain": "clinical_trials",
            "handler": "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
            "mode": mode,
        }
    )

    assert trace["qa_trace"]["canonical_router_cutover"] == {
        "fired": True,
        "domain": "clinical_trials",
        "capability": "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
        "mode": mode,
        "deterministic": is_deterministic,
    }


def test_trace_omits_canonical_cutover_when_selector_did_not_fire() -> None:
    trace = _trace(None)

    assert "canonical_router_cutover" not in trace["qa_trace"]


def test_cutover_projection_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = "관측 투영과 무관한 기존 답변"
    answer_sha = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        runtime_provenance,
        "_canonical_router_cutover_trace",
        lambda _diagnostics: (_ for _ in ()).throw(RuntimeError("injected projection failure")),
    )

    trace = trace_envelope(
        question="NCT01234567 임상시험 상세",
        result={
            "router_diagnostics": {
                "canonical_router_cutover": {
                    "domain": "clinical_trials",
                    "handler": "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
                    "mode": "deterministic",
                }
            },
            "tool_calls": [],
            "markdown_response": {"fact_md": "", "data_md": ""},
        },
        answer=answer,
        charts=(),
        timing={"stages": []},
        conversation_id="cutover-fail-open-test",
    )

    assert hashlib.sha256(answer.encode("utf-8")).hexdigest() == answer_sha
    assert trace["chart_count"] == 0
    assert "canonical_router_cutover" not in trace["qa_trace"]
