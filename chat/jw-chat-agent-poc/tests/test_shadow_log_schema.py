from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.operation_contract import (
    observe_actual_coverage,
    observe_plan_coverage,
    observe_surface_coverage,
    set_current_query_spec,
)
from jw_chat_agent_poc.orchestrator import shadow_gate_runtime as runtime
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
)


def _set_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")


def test_request_scope_reuses_request_id_and_rotates_observation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_shadow_mode(monkeypatch)
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_write_structured_payload", payloads.append)

    @runtime.shadow_request_scope
    def emit_three_phases() -> str:
        for phase in ("plan", "actual", "surface"):
            runtime.emit_shadow_gate_observation(
                gate=runtime.ShadowGate.OPERATION_CONTRACT,
                phase=phase,
                status="PASS",
                reason="covered",
            )
        return "public answer"

    assert emit_three_phases() == "public answer"
    assert len({payload["request_id"] for payload in payloads}) == 1
    assert payloads[0]["request_id"]
    assert len({payload["observation_id"] for payload in payloads}) == 3
    assert all(payload["observation_schema_version"] == 2 for payload in payloads)
    assert all("pod_name" in payload for payload in payloads)
    assert all("git_sha" in payload for payload in payloads)
    assert all("image_digest" in payload for payload in payloads)
    assert all(
        datetime.fromisoformat(str(payload["event_timestamp_utc"])).tzinfo is not None
        for payload in payloads
    )
    first_request_id = payloads[0]["request_id"]
    payloads.clear()

    assert emit_three_phases() == "public answer"
    assert len({payload["request_id"] for payload in payloads}) == 1
    assert payloads[0]["request_id"] != first_request_id
    assert runtime.current_shadow_request_id() == ""


def test_request_identity_failure_does_not_change_returned_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime,
        "uuid4",
        lambda: (_ for _ in ()).throw(RuntimeError("uuid unavailable")),
    )

    @runtime.shadow_request_scope
    def answer() -> str:
        return "byte-identical answer"

    assert answer().encode("utf-8") == b"byte-identical answer"


def test_g3_plan_actual_surface_events_inherit_one_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_shadow_mode(monkeypatch)
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_write_structured_payload", payloads.append)
    spec = RequestQuerySpec(
        entities=(
            QueryEntity(
                kind=EntityKind.BRAND,
                canonical_id="리바로",
                display_name="리바로",
            ),
        ),
        operation=QueryOperation.CURRENT_VALUE,
        metrics=("sales",),
    )
    plan = (
        ToolCallPlan(
            name="get_metric",
            arguments={"brand": "리바로", "measure": "sales", "period": "latest"},
        ),
    )
    calls = (
        {
            "status": "ok",
            "render_data": {
                "brand": "리바로",
                "metric": "sales",
                "sales_krw": 1,
                "period": "latest",
            },
        },
    )

    @runtime.shadow_request_scope
    def observe_all_phases() -> None:
        set_current_query_spec(spec, question_fingerprint="fingerprint")
        observe_plan_coverage(spec, plan, planner_kind="test", step=1)
        observe_actual_coverage(spec, calls, question_fingerprint="fingerprint")
        observe_surface_coverage(
            spec,
            "리바로 매출은 1원입니다.",
            calls,
            question_fingerprint="fingerprint",
            baseline_answer="리바로 매출은 1원입니다.",
            served_answer="리바로 매출은 1원입니다.",
        )

    observe_all_phases()

    operation_payloads = [
        payload
        for payload in payloads
        if payload["gate_name"] == "operation_contract"
    ]
    assert [payload["phase"] for payload in operation_payloads] == [
        "plan",
        "actual",
        "surface",
    ]
    assert len({payload["request_id"] for payload in operation_payloads}) == 1
    assert len({payload["observation_id"] for payload in operation_payloads}) == 3
    surface = operation_payloads[-1]
    assert surface["byte_match_baseline_served"] is True
    assert surface["candidate_available"] is False


def test_answer_parity_fields_hash_distinct_inputs() -> None:
    fields = runtime.answer_parity_fields(
        baseline_answer="baseline",
        served_answer="served",
        candidate_answer="candidate",
    )

    assert fields == {
        "baseline_answer_sha256": hashlib.sha256(b"baseline").hexdigest(),
        "served_answer_sha256": hashlib.sha256(b"served").hexdigest(),
        "candidate_answer_sha256": hashlib.sha256(b"candidate").hexdigest(),
        "byte_match_baseline_served": False,
        "candidate_byte_match": False,
        "candidate_available": True,
    }
    assert fields["candidate_answer_sha256"] != fields["served_answer_sha256"]


def test_answer_parity_fields_do_not_invent_candidate_hash() -> None:
    fields = runtime.answer_parity_fields(
        baseline_answer="same",
        served_answer="same",
        candidate_answer=None,
    )

    assert fields["byte_match_baseline_served"] is True
    assert fields["candidate_available"] is False
    assert fields["candidate_answer_sha256"] is None
    assert fields["candidate_byte_match"] is None


def test_structured_writer_failure_is_fail_open_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_shadow_mode(monkeypatch)
    before = runtime.shadow_gate_exception_count(
        runtime.ShadowGate.OPERATION_CONTRACT
    )
    monkeypatch.setattr(
        runtime,
        "_write_structured_payload",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("sink unavailable")),
    )

    runtime.emit_shadow_gate_observation(
        gate=runtime.ShadowGate.OPERATION_CONTRACT,
        phase="surface",
        status="PASS",
        reason="covered",
        baseline_answer="unchanged",
        served_answer="unchanged",
    )

    assert (
        runtime.shadow_gate_exception_count(runtime.ShadowGate.OPERATION_CONTRACT)
        == before + 1
    )


def test_hash_failure_emits_exception_observation_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_shadow_mode(monkeypatch)
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_write_structured_payload", payloads.append)
    monkeypatch.setattr(
        runtime,
        "_answer_sha256",
        lambda _answer: (_ for _ in ()).throw(RuntimeError("hash unavailable")),
    )

    runtime.emit_shadow_gate_observation(
        gate=runtime.ShadowGate.OPERATION_CONTRACT,
        phase="surface",
        status="PASS",
        reason="covered",
        baseline_answer="baseline",
        served_answer="served",
    )

    assert payloads[-1]["status"] == "EVALUATOR_EXCEPTION"
    assert payloads[-1]["evaluator_exception"] is True
    assert payloads[-1]["answer_action"] == "unchanged"


def test_structured_writer_emits_one_json_object_without_uvicorn_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime._write_structured_payload(
        {"event": "shadow_gate_observation", "gate_name": "operation_contract"}
    )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("{")
    assert lines[0].endswith("}")
    assert "uvicorn" not in lines[0]
    assert "shadow_gate_observation {" not in lines[0]
