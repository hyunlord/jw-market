from __future__ import annotations

import json
import logging
import os
from threading import Thread
from typing import Callable

from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    current_shadow_request_id,
    question_fingerprint,
)
from jw_chat_agent_poc.tool_use.v3_selection import (
    V3SelectionResult,
    V3ToolSelector,
)


LOGGER = logging.getLogger(__name__)


def run_v3_selection_shadow_once(
    question: str,
    *,
    selector: V3ToolSelector,
    request_id: str | None = None,
) -> dict[str, object]:
    result = selector.select(question)
    selected_tools = [choice.name for choice in result.choices]
    payload: dict[str, object] = {
        "event": "v3_tool_selection_shadow",
        "request_id": request_id or current_shadow_request_id(),
        "question_fingerprint": question_fingerprint(question),
        "status": "ok",
        "candidate_count": len(result.candidate_names),
        "candidate_names": list(result.candidate_names),
        "selected_tools": selected_tools,
        "selection_count": len(selected_tools),
        "provider_choice_count": result.provider_choice_count,
        "unknown_tool_names": list(result.unknown_tool_names),
        "intent_domains": list(result.intent.domains),
        "intent_operations": list(result.intent.operations),
        "presentation": result.intent.presentation,
        "internal_tool_execution_count": 0,
        "consumed_by_serving_path": False,
        "answer_action": "unchanged",
    }
    LOGGER.info(
        "v3_tool_selection_shadow_observed payload=%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )
    _observe_v3_execution_shadow(
        question,
        result,
        request_id=str(payload["request_id"] or ""),
    )
    return payload


def start_v3_selection_shadow(
    question: str,
    *,
    request_id: str | None = None,
    selector_factory: Callable[[], V3ToolSelector] | None = None,
) -> Thread:
    bound_request_id = request_id or current_shadow_request_id()
    factory = selector_factory or _default_selector

    def worker() -> None:
        try:
            run_v3_selection_shadow_once(
                question,
                selector=factory(),
                request_id=bound_request_id,
            )
        except Exception as exc:
            payload = {
                "event": "v3_tool_selection_shadow",
                "request_id": bound_request_id,
                "question_fingerprint": question_fingerprint(question),
                "status": "error",
                "error_name": type(exc).__name__,
                "internal_tool_execution_count": 0,
                "consumed_by_serving_path": False,
                "answer_action": "unchanged",
            }
            LOGGER.exception(
                "v3_tool_selection_shadow_failed payload=%s",
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )

    thread = Thread(target=worker, name="v3-tool-selection-shadow", daemon=True)
    thread.start()
    return thread


def _default_selector() -> V3ToolSelector:
    from jw_chat_agent_poc.tool_use.v3_selection_provider import (
        GenosV3ToolChoiceProvider,
    )

    return V3ToolSelector(provider=GenosV3ToolChoiceProvider.from_env())


def _observe_v3_execution_shadow(
    question: str,
    result: V3SelectionResult,
    *,
    request_id: str,
) -> None:
    if os.environ.get("JW_CHAT_V3_TOOL_EXECUTION_SHADOW", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from jw_chat_agent_poc.tool_use.v3_execution_shadow import (
            start_v3_execution_shadow,
        )

        start_v3_execution_shadow(
            question,
            result,
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 - execution SHADOW cannot affect selection or answer
        LOGGER.exception("v3_tool_execution_shadow_start_failed")
