from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jw_chat_agent_poc.contracts.routing import RouteDecision
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    current_shadow_request_id,
    question_fingerprint,
)
_LOGGER = logging.getLogger(__name__)


def emit_route_decision(decision: RouteDecision, *, question: str) -> None:
    """Emit a routing observation without participating in routing."""

    try:
        from jw_chat_agent_poc.service.runtime_provenance import release_identity_payload

        identity = release_identity_payload()
        _write_route_decision_payload(
            {
                "event": "route_decision_observation",
                "observation_schema_version": 1,
                "request_id": current_shadow_request_id(),
                "observation_id": uuid4().hex,
                "event_timestamp_utc": datetime.now(UTC).isoformat(),
                "pod_name": os.environ.get("HOSTNAME") or "unknown",
                "git_sha": identity["git_sha"],
                "image_digest": identity["image_digest"],
                "mode": "SHADOW",
                "answer_action": "unchanged",
                "question_fingerprint": question_fingerprint(question),
                "route_decision": decision.model_dump(mode="json"),
            }
        )
    except Exception:  # noqa: BLE001 - telemetry must not affect routing or output
        _LOGGER.exception("route_decision_shadow_emit_failed")


def observe_route_decision(*, question: str, **fields: Any) -> None:
    """Build and emit a decision fail-open for low-friction call sites."""

    try:
        emit_route_decision(RouteDecision(**fields), question=question)
    except Exception:  # noqa: BLE001 - telemetry must not affect routing or output
        _LOGGER.exception("route_decision_shadow_build_failed")


def _write_route_decision_payload(payload: dict[str, object]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write(f"{serialized}\n")
    sys.stdout.flush()
