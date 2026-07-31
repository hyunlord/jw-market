from __future__ import annotations

import copy
import json
import logging

import pytest

from jw_chat_agent_poc.orchestrator.operation_contract import (
    observe_actual_coverage,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
)
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    ShadowGate,
    ShadowGateMode,
    shadow_gate_mode,
)
from jw_chat_agent_poc.orchestrator.typed_failure import observe_typed_failure
from jw_chat_agent_poc.service.app import compute_final_answer


_MODE_ENVS = (
    "JW_CHAT_OPERATION_CONTRACT_MODE",
    "JW_CHAT_PERIOD_SET_CONTRACT_MODE",
    "JW_CHAT_TYPED_FAILURE_MODEL_MODE",
)


def _log_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    prefix = "shadow_gate_observation "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


def _set_all_modes(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    for name in _MODE_ENVS:
        monkeypatch.setenv(name, value)


def _sales_spec(
    *,
    entities: tuple[str, ...] = ("리바로",),
    metrics: tuple[str, ...] = ("sales",),
    start_period: str | None = None,
    end_period: str | None = None,
) -> RequestQuerySpec:
    return RequestQuerySpec(
        operation=(
            QueryOperation.COMPARE_CURRENT
            if len(entities) > 1
            else QueryOperation.CURRENT_VALUE
        ),
        entities=tuple(
            QueryEntity(
                kind=EntityKind.BRAND,
                canonical_id=value,
                display_name=value,
            )
            for value in entities
        ),
        metrics=metrics,
        start_period=start_period,
        end_period=end_period,
        comparison_targets=(
            tuple(
                QueryEntity(
                    kind=EntityKind.BRAND,
                    canonical_id=value,
                    display_name=value,
                )
                for value in entities
            )
            if len(entities) > 1
            else ()
        ),
    )


def test_shadow_gate_modes_default_to_shadow_and_invalid_values_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _MODE_ENVS:
        monkeypatch.delenv(name, raising=False)

    assert shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is ShadowGateMode.SHADOW
    assert shadow_gate_mode(ShadowGate.PERIOD_SET) is ShadowGateMode.SHADOW
    assert shadow_gate_mode(ShadowGate.TYPED_FAILURE_MODEL) is ShadowGateMode.SHADOW

    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "off")
    assert shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is ShadowGateMode.OFF

    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "enforce")
    assert shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is ShadowGateMode.ENFORCE

    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "unsupported")
    assert shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is ShadowGateMode.OFF


def test_operation_and_period_observations_are_structured_and_aggregatable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_all_modes(monkeypatch, "SHADOW")
    caplog.set_level(logging.INFO)

    decision = observe_actual_coverage(
        _sales_spec(start_period="2024-01", end_period="2024-03"),
        [
            {
                "status": "ok",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "sales_krw": 1,
                    "period": "2024-01",
                },
            }
        ],
    )

    payloads = _log_payloads(caplog)
    operation = next(
        payload for payload in payloads if payload["gate"] == "operation_contract"
    )
    period = next(payload for payload in payloads if payload["gate"] == "period_set")

    assert operation["mode"] == "SHADOW"
    assert operation["phase"] == "actual"
    assert operation["status"] == decision.status.value.upper()
    assert operation["missing_count"] == len(decision.missing)

    assert period["mode"] == "SHADOW"
    assert period["phase"] == "actual"
    assert period["status"] == decision.period_coverage.status.value.upper()
    assert period["required_count"] == decision.period_coverage.selection.expected_count
    assert period["observed_count"] == len(decision.period_coverage.observed)
    assert period["missing_count"] == len(decision.period_coverage.missing)

    serialized = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("shadow_gate_observation ")
    )
    assert "리바로" not in serialized


def test_off_mode_suppresses_logs_without_changing_decision(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_all_modes(monkeypatch, "OFF")
    caplog.set_level(logging.INFO)
    spec = _sales_spec()
    calls = [
        {
            "status": "ok",
            "render_data": {
                "brand": "리바로",
                "metric": "sales",
                "sales_krw": 1,
                "period": "latest",
            },
        }
    ]

    decision = observe_actual_coverage(spec, calls)

    assert decision.status.value == "pass"
    assert _log_payloads(caplog) == []


def test_typed_failure_observation_is_structured_and_does_not_log_answer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_all_modes(monkeypatch, "SHADOW")
    caplog.set_level(logging.INFO)
    result = {
        "error_code": "UPSTREAM_UNAVAILABLE",
        "answer": "사용자에게만 보여야 하는 상세 안내",
    }

    normalized = observe_typed_failure(result)

    assert normalized is not None
    payload = next(
        payload
        for payload in _log_payloads(caplog)
        if payload["gate"] == "typed_failure_model"
    )
    assert payload["mode"] == "SHADOW"
    assert payload["phase"] == "final"
    assert payload["status"] == "MATCHED"
    assert payload["reason"] == "UPSTREAM_UNAVAILABLE"
    assert "사용자에게만" not in "\n".join(
        record.getMessage() for record in caplog.records
    )


def test_typed_failure_observer_error_cannot_change_public_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jw_chat_agent_poc.service import app as service_app

    monkeypatch.setenv("JW_CHAT_RESPONSE_FORMAT_CONTRACT", "OFF")
    monkeypatch.setattr(
        service_app,
        "observe_typed_failure",
        lambda _result: (_ for _ in ()).throw(RuntimeError("observer failure")),
    )
    result = {
        "general_view_ready": True,
        "answer": "관측 실패와 무관하게 유지되는 답변입니다.",
        "sources": ["UBIST"],
        "tool_calls": [],
    }

    final = compute_final_answer(
        "typed observer 격리",
        result,
        conversation_id="shadow-observer-isolation",
    )

    assert "관측 실패와 무관하게 유지되는 답변입니다." in final.text


@pytest.mark.parametrize(
    ("answer", "query_spec"),
    (
        ("리바로 최신 매출입니다.", _sales_spec()),
        (
            "리바로와 리피토의 최신 매출 비교입니다.",
            _sales_spec(entities=("리바로", "리피토")),
        ),
        ("리바로 매출과 점유율입니다.", _sales_spec(metrics=("sales", "share"))),
        ("리바로 시장 순위입니다.", _sales_spec(metrics=("rank",))),
        (
            "리바로 2024년 1월 매출입니다.",
            _sales_spec(start_period="2024-01", end_period="2024-01"),
        ),
        (
            "리바로 2024년 월별 매출입니다.",
            _sales_spec(start_period="2024-01", end_period="2024-12"),
        ),
        (
            "아일리아 최근 네 분기 매출입니다.",
            _sales_spec(start_period="2023-Q4", end_period="2024-Q3"),
        ),
        ("급여기준 상류 연결 상태를 확인할 수 없습니다.", None),
    ),
)
def test_all_runtime_modes_leave_public_answer_bytes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    query_spec: RequestQuerySpec | None,
) -> None:
    monkeypatch.setenv("JW_CHAT_RESPONSE_FORMAT_CONTRACT", "OFF")
    result = {
        "general_view_ready": True,
        "answer": answer,
        "sources": ["UBIST"],
        "tool_calls": [],
    }
    if query_spec is None:
        result["error_code"] = "UPSTREAM_UNAVAILABLE"

    outputs = []
    for mode in ("OFF", "SHADOW", "ENFORCE"):
        _set_all_modes(monkeypatch, mode)
        outputs.append(
            compute_final_answer(
                "관측 모드별 동일 응답 검증",
                copy.deepcopy(result),
                conversation_id=f"shadow-byte-{mode.lower()}",
                query_spec=query_spec,
            )
        )

    assert outputs[0].text.encode("utf-8") == outputs[1].text.encode("utf-8")
    assert outputs[1].text.encode("utf-8") == outputs[2].text.encode("utf-8")
    assert outputs[0].sources == outputs[1].sources == outputs[2].sources
    assert outputs[0].charts == outputs[1].charts == outputs[2].charts
