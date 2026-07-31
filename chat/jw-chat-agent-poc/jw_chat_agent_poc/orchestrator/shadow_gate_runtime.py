from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
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
# Uvicorn owns the service stdout handler; module loggers at INFO are not
# collected by the deployed logging configuration.
_LOGGER = logging.getLogger("uvicorn.error")


def shadow_gate_mode(gate: ShadowGate) -> ShadowGateMode:
    raw = os.environ.get(_MODE_ENVS[gate])
    if raw is None:
        return ShadowGateMode.SHADOW
    try:
        return ShadowGateMode(raw.strip().upper())
    except ValueError:
        return ShadowGateMode.OFF


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
) -> None:
    mode = shadow_gate_mode(gate)
    if mode is ShadowGateMode.OFF:
        return

    payload: dict[str, str | int | bool | None] = {
        "event": "shadow_gate_observation",
        "gate": gate.value,
        "phase": phase,
        "mode": mode.value,
        "status": status.upper(),
        "reason": reason,
        "required_count": required_count,
        "observed_count": observed_count,
        "missing_count": missing_count,
        "terminal": terminal,
        "partial": partial,
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
