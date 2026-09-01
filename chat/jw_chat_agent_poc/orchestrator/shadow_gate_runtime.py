from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from threading import Lock
from typing import Any, Callable, Final, ParamSpec, TypeVar
from uuid import uuid4


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
_OBSERVATION_SCHEMA_VERSION: Final = 2
_EXCEPTION_COUNTS: Final[dict[ShadowGate, int]] = {
    gate: 0 for gate in ShadowGate
}
_EXCEPTION_COUNTS_LOCK: Final = Lock()
_REQUEST_ID: Final[ContextVar[str]] = ContextVar(
    "shadow_gate_request_id",
    default="",
)
_LOGGER = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")


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


def current_shadow_request_id() -> str:
    return _REQUEST_ID.get()


def shadow_request_scope(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Give all observations from one request a shared, fail-open identity."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        token: Token[str] | None = None
        try:
            token = _REQUEST_ID.set(uuid4().hex)
        except Exception:  # noqa: BLE001 - telemetry cannot affect the answer
            _LOGGER.exception("shadow_request_context_start_failed")
        try:
            return function(*args, **kwargs)
        finally:
            if token is not None:
                try:
                    _REQUEST_ID.reset(token)
                except Exception:  # noqa: BLE001 - telemetry cannot affect the answer
                    _LOGGER.exception("shadow_request_context_reset_failed")

    return wrapped


@contextmanager
def shadow_request_id_scope(request_id: str | None):
    """Restore a stored request identity while deferred answers are finalized."""

    token: Token[str] | None = None
    if request_id:
        try:
            token = _REQUEST_ID.set(request_id)
        except Exception:  # noqa: BLE001 - telemetry cannot affect the answer
            _LOGGER.exception("shadow_request_context_restore_failed")
    try:
        yield
    finally:
        if token is not None:
            try:
                _REQUEST_ID.reset(token)
            except Exception:  # noqa: BLE001 - telemetry cannot affect the answer
                _LOGGER.exception("shadow_request_context_restore_reset_failed")


def answer_parity_fields(
    *,
    baseline_answer: str,
    served_answer: str,
    candidate_answer: str | None,
) -> dict[str, str | bool | None]:
    baseline_hash = _answer_sha256(baseline_answer)
    served_hash = _answer_sha256(served_answer)
    candidate_hash = (
        _answer_sha256(candidate_answer) if candidate_answer is not None else None
    )
    return {
        "baseline_answer_sha256": baseline_hash,
        "served_answer_sha256": served_hash,
        "candidate_answer_sha256": candidate_hash,
        "byte_match_baseline_served": baseline_hash == served_hash,
        "candidate_byte_match": (
            candidate_hash == served_hash if candidate_hash is not None else None
        ),
        "candidate_available": candidate_hash is not None,
    }


def _answer_sha256(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


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
    baseline_answer: str | None = None,
    served_answer: str | None = None,
    candidate_answer: str | None = None,
) -> None:
    mode = shadow_gate_mode(gate)
    if mode is ShadowGateMode.OFF:
        return

    try:
        payload: dict[str, Any] = {
            "event": "shadow_gate_observation",
            "observation_schema_version": _OBSERVATION_SCHEMA_VERSION,
            "request_id": current_shadow_request_id(),
            "observation_id": uuid4().hex,
            "event_timestamp_utc": datetime.now(UTC).isoformat(),
            **_runtime_identity(),
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
        if phase == "surface" and baseline_answer is not None and served_answer is not None:
            payload.update(
                answer_parity_fields(
                    baseline_answer=baseline_answer,
                    served_answer=served_answer,
                    candidate_answer=candidate_answer,
                )
            )
        _write_structured_payload(payload)
    except Exception:  # noqa: BLE001 - observation failure is fail-open by contract
        _record_observation_failure(gate=gate, phase=phase)
        _LOGGER.exception("shadow_gate_observation_emit_failed")


def _runtime_identity() -> dict[str, str]:
    from jw_chat_agent_poc.service.runtime_provenance import release_identity_payload

    identity = release_identity_payload()
    return {
        "pod_name": os.environ.get("HOSTNAME") or "unknown",
        "git_sha": identity["git_sha"],
        "image_digest": identity["image_digest"],
    }


def _write_structured_payload(payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write(f"{serialized}\n")
    sys.stdout.flush()


def _record_observation_failure(*, gate: ShadowGate, phase: str) -> None:
    with _EXCEPTION_COUNTS_LOCK:
        _EXCEPTION_COUNTS[gate] += 1
        exception_count = _EXCEPTION_COUNTS[gate]
    try:
        _write_structured_payload(
            {
                "event": "shadow_gate_observation",
                "observation_schema_version": _OBSERVATION_SCHEMA_VERSION,
                "request_id": current_shadow_request_id(),
                "observation_id": uuid4().hex,
                "event_timestamp_utc": datetime.now(UTC).isoformat(),
                "pod_name": os.environ.get("HOSTNAME") or "unknown",
                "git_sha": "unknown",
                "image_digest": "unknown",
                "gate": gate.value,
                "gate_name": gate.value,
                "gate_version": _GATE_VERSION,
                "phase": phase,
                "mode": shadow_gate_mode(gate).value,
                "status": "EVALUATOR_EXCEPTION",
                "reason": "observation_emit_failed",
                "evaluator_exception": True,
                "exception_count": exception_count,
                "answer_action": "unchanged",
            }
        )
    except Exception:  # noqa: BLE001 - an unavailable sink cannot block the answer
        return
