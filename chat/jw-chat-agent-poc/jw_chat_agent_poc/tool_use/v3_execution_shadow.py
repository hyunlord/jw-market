from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from threading import Thread
from typing import Protocol, TYPE_CHECKING

from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    current_shadow_request_id,
    question_fingerprint,
)

if TYPE_CHECKING:
    from jw_chat_agent_poc.tool_use.v3_execution import V3EvidenceBundle
    from jw_chat_agent_poc.tool_use.v3_selection import (
        MultiToolChoice,
        V3SelectionResult,
    )


LOGGER = logging.getLogger(__name__)
EXECUTION_SHADOW_ENV = "JW_CHAT_V3_TOOL_EXECUTION_SHADOW"


class _Executor(Protocol):
    def execute(self, choices: Sequence[MultiToolChoice]) -> V3EvidenceBundle: ...


def execution_shadow_enabled() -> bool:
    return os.environ.get(EXECUTION_SHADOW_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_v3_execution_shadow_once(
    question: str,
    selection: V3SelectionResult,
    *,
    request_id: str | None = None,
    executor_factory: Callable[[str], _Executor] | None = None,
) -> dict[str, object]:
    bound_request_id = request_id or current_shadow_request_id()
    if not execution_shadow_enabled():
        return {
            "event": "v3_tool_execution_shadow",
            "request_id": bound_request_id,
            "status": "disabled",
            "consumed_by_serving_path": False,
            "answer_generation_count": 0,
        }
    try:
        factory = executor_factory or _default_executor_factory
        bundle = factory(question).execute(selection.choices)
        payload = {
            "event": "v3_tool_execution_shadow",
            "request_id": bound_request_id,
            "question_fingerprint": question_fingerprint(question),
            **bundle.summary(),
        }
        LOGGER.info(
            "v3_tool_execution_shadow_observed payload=%s",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
        return payload
    except Exception as exc:  # noqa: BLE001 - execution SHADOW must fail open
        payload = {
            "event": "v3_tool_execution_shadow",
            "request_id": bound_request_id,
            "question_fingerprint": question_fingerprint(question),
            "status": "error",
            "error_name": type(exc).__name__,
            "consumed_by_serving_path": False,
            "answer_generation_count": 0,
        }
        LOGGER.exception(
            "v3_tool_execution_shadow_failed payload=%s",
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        return payload


def start_v3_execution_shadow(
    question: str,
    selection: V3SelectionResult,
    *,
    request_id: str | None = None,
    executor_factory: Callable[[str], _Executor] | None = None,
) -> Thread:
    """Detach tool execution from both serving and selection observation."""

    bound_request_id = request_id or current_shadow_request_id()

    def worker() -> None:
        run_v3_execution_shadow_once(
            question,
            selection,
            request_id=bound_request_id,
            executor_factory=executor_factory,
        )

    thread = Thread(target=worker, name="v3-tool-execution-shadow", daemon=True)
    thread.start()
    return thread


def _default_executor_factory(question: str) -> _Executor:
    from jw_chat_agent_poc.tool_use.v3_execution import build_default_shadow_executor

    return build_default_shadow_executor(question)
