from __future__ import annotations

import hashlib
import json
import logging
import os
from enum import StrEnum
from threading import Lock
from typing import Final


class ShadowGate(StrEnum):
    OPERATION_CONTRACT = "operation_contract"
    PERIOD_SET = "period_set"
    TYPED_FAILURE_MODEL = "typed_failure_model"


class ShadowGateMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


_MODE_ENVS: Final[dict[ShadowGate, str]] = {
    ShadowGate.OPERATION_CONTRACT: "JW_CHAT_OPERATION_CONTRACT_MODE",
    ShadowGate.PERIOD_SET: "JW_CHAT_PERIOD_SET_CONTRACT_MODE",
    ShadowGate.TYPED_FAILURE_MODEL: "JW_CHAT_TYPED_FAILURE_MODEL_MODE",
}
_GATE_VERSION: Final = 1
_EXCEPTION_COUNTS: Final[dict[ShadowGate, int]] = {
    gate: 0 for gate in ShadowGate
}
_EXCEPTION_COUNTS_LOCK: Final = Lock()
# Uvicorn owns the service stdout handler; module loggers at INFO are not
# collected by the deployed logging configuration.
_LOGGER = logging.getLogger("uvicorn.error")


def shadow_gate_mode(gate: ShadowGate) -> ShadowGateMode:
    raw = os.environ.get(_MODE_ENVS[gate])
    if raw is None:
        return ShadowGateMode.OFF
    try:
        return ShadowGateMode(raw.strip().upper())
    except ValueError:
        return ShadowGateMode.OFF


def question_fingerprint(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def shadow_gate_exception_count(gate: ShadowGate) -> int:
    with _EXCEPTION_COUNTS_LOCK:
        return _EXCEPTION_COUNTS[gate]


def emit_shadow_gate_exception(
    *,
    gate: ShadowGate,
    phase: str,
    question_fingerprint: str,
    entity_count: int = 0,
    metric_count: int = 0,
    period_count: int = 0,
) -> None:
    with _EXCEPTION_COUNTS_LOCK:
        _EXCEPTION_COUNTS[gate] += 1
        exception_count = _EXCEPTION_COUNTS[gate]
    emit_shadow_gate_observation(
        gate=gate,
        phase=phase,
        status="EVALUATOR_EXCEPTION",
        reason="evaluator_exception",
        question_fingerprint=question_fingerprint,
        entity_count=entity_count,
        metric_count=metric_count,
        period_count=period_count,
        evaluator_exception=True,
        exception_count=exception_count,
    )


def emit_shadow_gate_observation(
    *,
    gate: ShadowGate,
    phase: str,
    status: str,
    reason: str,
    required_count: int | None = None,
    observed_count: int | None = None,
    missing_count: int | None = None,
    terminal: bool | None = None,
    partial: bool | None = None,
    entity_count: int = 0,
    metric_count: int = 0,
    period_count: int = 0,
    evaluator_exception: bool = False,
    exception_count: int | None = None,
    answer_action: str = "unchanged",
    question_fingerprint: str = "",
) -> None:
    mode = shadow_gate_mode(gate)
    if mode is ShadowGateMode.OFF:
        return

    payload: dict[str, str | int | bool | None] = {
        "event": "shadow_gate_observation",
        "gate": gate.value,
        "gate_name": gate.value,
        "gate_version": _GATE_VERSION,
        "phase": phase,
        "mode": mode.value,
        "status": status.upper(),
        "reason": reason,
        "required_count": required_count,
        "observed_count": observed_count,
        "missing_count": missing_count,
        "terminal": terminal,
        "partial": partial,
        "entity_count": entity_count,
        "metric_count": metric_count,
        "period_count": period_count,
        "evaluator_exception": evaluator_exception,
        "exception_count": exception_count,
        "answer_action": answer_action,
        "question_fingerprint": question_fingerprint,
    }
    _LOGGER.info(
        "shadow_gate_observation %s",
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
