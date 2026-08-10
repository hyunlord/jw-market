from __future__ import annotations

import time
from collections.abc import Sequence
from uuid import uuid4

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.adapters import build_source_adapters
from jw_chat_agent_poc.service.v4.contracts import SourceResult, V4Answer
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer


class V4Runtime:
    def __init__(
        self,
        *,
        planner: V4Planner,
        executor: ParallelSourceExecutor,
        synthesizer: V4Synthesizer,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._synthesizer = synthesizer
        self._total_timeout_s = 54.0

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None,
        turns: Sequence[ConversationTurn],
    ) -> V4Answer:
        started = time.monotonic()
        deadline = started + self._total_timeout_s
        selected_turns = tuple(turns)[-10:]
        session_id = conversation_id or uuid4().hex
        plan = self._planner.plan(
            question,
            selected_turns,
            budget_s=min(18.0, _remaining(deadline)),
        )
        first_results = self._executor.execute(
            plan,
            session_id=session_id,
            total_timeout_s=min(20.0, _remaining(deadline)),
        )
        linked_plan = (
            self._planner.link(
                plan,
                first_results,
                selected_turns,
                budget_s=min(7.0, _remaining(deadline)),
            )
            if plan.needs_second_hop and _remaining(deadline) > 1.0
            else None
        )
        linked_results = (
            self._executor.execute(
                linked_plan,
                session_id=session_id,
                total_timeout_s=min(10.0, _remaining(deadline)),
            )
            if linked_plan is not None and _remaining(deadline) > 0.1
            else ()
        )
        results = tuple(_mark_citations_used(result) for result in (*first_results, *linked_results))
        synthesized = self._synthesizer.synthesize(
            plan,
            results,
            selected_turns,
            budget_s=min(15.0, _remaining(deadline)),
        )
        gated = apply_v4_gates(plan.resolved_question, synthesized, results)
        elapsed_ms = (time.monotonic() - started) * 1000
        trace = {
            "v4": True,
            "planner_serving": getattr(self._planner, "serving_id", "unknown"),
            "fallback": plan.linking_plan.startswith("planner fallback;"),
            "planner": plan.model_dump(mode="json"),
            "second_hop": linked_plan.model_dump(mode="json") if linked_plan else None,
            "tool_results": [
                {
                    "source": result.source,
                    "query": result.query,
                    "status": result.status,
                    "elapsed_ms": result.elapsed_ms,
                    "cache_hit": result.cache_hit,
                    "notice": result.notice,
                    "citations": [
                        citation.model_dump(mode="json")
                        for citation in result.citations
                    ],
                    "payload": result.payload,
                }
                for result in results
            ],
            "gates": gated.trace,
        }
        sources = tuple(
            dict.fromkeys(
                citation.source
                for result in results
                if result.status == "ok"
                for citation in result.citations
            )
        )
        return V4Answer(
            text=gated.text,
            sources=sources,
            trace=trace,
            timing={"total_elapsed_ms": elapsed_ms},
            conversation_id=session_id,
        )


def build_default_runtime() -> V4Runtime:
    return V4Runtime(
        planner=V4Planner(planner_client()),
        executor=ParallelSourceExecutor(
            adapters=build_source_adapters(),
            per_tool_timeout_s=20.0,
            total_timeout_s=20.0,
        ),
        synthesizer=V4Synthesizer(synthesizer_client()),
    )


def _remaining(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _mark_citations_used(result: SourceResult) -> SourceResult:
    if result.status != "ok":
        return result
    return result.model_copy(
        update={
            "citations": tuple(
                citation.model_copy(update={"used": True})
                for citation in result.citations
            )
        }
    )
