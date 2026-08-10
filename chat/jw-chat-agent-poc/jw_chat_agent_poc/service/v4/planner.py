from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence

import requests

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult, ToolQueries
from jw_chat_agent_poc.service.v4.llm import GenOSV4Client


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class V4Planner:
    def __init__(self, client: GenOSV4Client) -> None:
        self._client = client

    @property
    def serving_id(self) -> str:
        return self._client.serving_id

    def plan(
        self,
        question: str,
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 10.0,
    ) -> PlannerOutput:
        messages = _planner_messages(question, turns)
        error: Exception | None = None
        deadline = time.monotonic() + max(1.0, budget_s)
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = self._client.complete(
                    messages
                    + ([{"role": "system", "content": "The prior output was invalid. Return only valid JSON matching the schema."}] if attempt else []),
                    budget_s=remaining,
                )
                return _parse_plan(raw)
            except requests.RequestException as exc:
                error = exc
                break
            except (ValueError, json.JSONDecodeError) as exc:
                error = exc
        return _fallback_plan(question, turns, reason=str(error or "planner returned no JSON"))

    def link(
        self,
        first_plan: PlannerOutput,
        first_results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 7.0,
    ) -> PlannerOutput | None:
        if not first_plan.needs_second_hop:
            return None
        summaries = [
            {
                "source": result.source,
                "query": result.query,
                "status": result.status,
                "payload": result.payload,
            }
            for result in first_results
            if result.status == "ok"
        ]
        messages = _planner_messages(first_plan.resolved_question, turns)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Create the one allowed linking hop. Return the same strict JSON schema. "
                    "Set needs_second_hop=false. Use first-hop entities to make more precise queries.\n"
                    + json.dumps(summaries, ensure_ascii=False, default=str)
                ),
            }
        )
        try:
            linked = _parse_plan(self._client.complete(messages, budget_s=budget_s))
        except (ValueError, json.JSONDecodeError, requests.RequestException):
            return None
        return linked.model_copy(update={"needs_second_hop": False})


def _planner_messages(question: str, turns: Sequence[ConversationTurn]) -> list[dict[str, str]]:
    history = [
        {"question": turn.question, "answer": turn.answer}
        for turn in tuple(turns)[-10:]
    ]
    schema = PlannerOutput.model_json_schema()
    return [
        {
            "role": "system",
            "content": (
                "You are CHAT-V4's query planner. Resolve Korean anaphora from at most ten prior turns, "
                "expand the user's meaning, and produce queries for every source. You do not select tools: "
                "all seven keys mart, nedrug, hira, openfda, clinicaltrials, web, patent must contain at least "
                "one useful query. Until patent tooling is implemented, make patent a web-search query. "
                "Return JSON only, with no markdown. Set needs_second_hop only when first-hop entities are "
                "needed for one additional linking round. Schema: "
                + json.dumps(schema, ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"question": question, "recent_turns": history},
                ensure_ascii=False,
            ),
        },
    ]


def _parse_plan(raw: str) -> PlannerOutput:
    cleaned = _JSON_FENCE_RE.sub("", raw.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("planner output has no JSON object")
    return PlannerOutput.model_validate_json(cleaned[start : end + 1])


def _fallback_plan(
    question: str,
    turns: Sequence[ConversationTurn],
    *,
    reason: str,
) -> PlannerOutput:
    previous = turns[-1].question if turns else ""
    resolved = f"{previous} -> {question}" if previous else question
    return PlannerOutput(
        resolved_question=resolved,
        expanded_intents=("시장 지표", "공식 의약 정보", "외부 최신 근거"),
        tool_queries=ToolQueries(
            mart=(question,),
            nedrug=(f"{question} 의약품 허가 효능 성분",),
            hira=(f"{question} 급여기준 환자 통계",),
            openfda=(f"{question} label safety",),
            clinicaltrials=(f"{question} clinical trials",),
            web=(question,),
            patent=(f"{question} 특허 만료",),
        ),
        linking_plan=f"planner fallback; no second hop: {reason[:160]}",
        needs_second_hop=False,
    )
