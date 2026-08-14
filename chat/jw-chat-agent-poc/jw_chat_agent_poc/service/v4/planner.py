from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    ClinicalTrialConcept,
    PlannerOutput,
    QueryScope,
    RequestedAnswerShape,
    SourceName,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.clinical import (
    compile_clinical_query,
    concept_from_query,
)
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    is_query_entity_candidate,
    query_entity_candidates,
)
from jw_chat_agent_poc.service.v4.llm import CompletionResult, GenOSV4Client
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.time_context import (
    as_of_date_instruction,
    current_kst_date as _current_kst_date,
)
from jw_chat_agent_poc.service.v4.query_scope import (
    SOURCE_CALL_LIMIT,
    configured_entity_limit,
)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_NCT_ANCHOR_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_PRODUCT_CODE_ANCHOR_RE = re.compile(
    r"(?:품목기준코드|품목일련번호|ITEM_SEQ|PRDLST_STDR_CODE)\s*[:#]?\s*([A-Za-z0-9-]{6,32})",
    re.IGNORECASE,
)
_KCD_ANCHOR_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]\d{2}(?:\.?\d)?)(?![A-Za-z0-9])")
_RELATIVE_YEAR_RE = re.compile(r"최근\s*(?P<count>\d{1,2})\s*년")
_EXPLICIT_YEAR_RANGE_RE = re.compile(
    r"20\d{2}\s*년?\s*(?:~|～|부터|[-–—])\s*20\d{2}\s*년?"
)


@dataclass(frozen=True)
class PlannerOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]


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
        state: SessionState | None = None,
    ) -> PlannerOutput:
        return self.plan_with_trace(question, turns, budget_s=budget_s, state=state).plan

    def plan_with_trace(
        self,
        question: str,
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 10.0,
        state: SessionState | None = None,
    ) -> PlannerOutcome:
        observed_on = _current_kst_date()
        messages = _planner_messages(
            question,
            turns,
            state=state,
            observed_on=observed_on,
        )
        error: Exception | None = None
        deadline = time.monotonic() + max(1.0, budget_s)
        started = time.monotonic()
        completion: CompletionResult | None = None
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                attempt_messages = messages + (
                    [{"role": "system", "content": "The prior output was invalid. Return only valid JSON matching the schema."}]
                    if attempt
                    else []
                )
                detailed = getattr(self._client, "complete_detailed", None)
                if callable(detailed):
                    completion = detailed(
                        attempt_messages,
                        budget_s=remaining,
                        max_tokens=4096,
                    )
                    raw = completion.text
                else:
                    raw = self._client.complete(attempt_messages, budget_s=remaining)
                plan = _parse_plan(raw)
                direct_sources = _direct_answer_sources(question)
                if direct_sources:
                    plan = plan.model_copy(update={"answer_sources": direct_sources})
                plan = _lock_exact_anchor(question, plan)
                plan = _anchor_relative_years(question, plan, observed_on)
                plan = _attach_lossless_contracts(question, plan)
                plan = _limit_first_wave_queries(plan)
                return PlannerOutcome(
                    plan=plan,
                    trace={
                        "status": "ok",
                        "elapsed_ms": completion.elapsed_ms if completion else (time.monotonic() - started) * 1000,
                        "finish_reason": completion.finish_reason if completion else None,
                        "usage": _normalized_usage(completion.usage if completion else {}),
                        "serving_id": completion.serving_id if completion else "not_applicable",
                        "model": completion.model if completion else "not_applicable",
                    },
                )
            except requests.RequestException as exc:
                error = exc
                break
            except (ValueError, json.JSONDecodeError) as exc:
                error = exc
        return PlannerOutcome(
            plan=_attach_lossless_contracts(
                question,
                _anchor_relative_years(
                    question,
                    _lock_exact_anchor(
                        question,
                        _fallback_plan(
                            question,
                            turns,
                            reason=str(error or "planner returned no JSON"),
                        ),
                    ),
                    observed_on,
                ),
            ),
            trace={
                "status": "fallback",
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "finish_reason": completion.finish_reason if completion else None,
                "usage": _normalized_usage(completion.usage if completion else {}),
                "serving_id": completion.serving_id if completion else "not_applicable",
                "model": completion.model if completion else "not_applicable",
                "error_type": type(error).__name__ if error else "InvalidPlannerOutput",
            },
        )

    def link(
        self,
        first_plan: PlannerOutput,
        first_results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 7.0,
        state: SessionState | None = None,
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
        observed_on = _current_kst_date()
        messages = _planner_messages(
            first_plan.resolved_question,
            turns,
            state=state,
            observed_on=observed_on,
        )
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
        anchor = _exact_anchor(first_plan.resolved_question)
        if anchor:
            linked = _lock_linked_anchor(anchor, linked, first_results)
        linked = linked.model_copy(update={"needs_second_hop": False})
        return _anchor_relative_years(first_plan.resolved_question, linked, observed_on)


def _exact_anchor(question: str) -> str | None:
    nct = _NCT_ANCHOR_RE.search(question)
    if nct:
        return nct.group(0).upper()
    product_code = _PRODUCT_CODE_ANCHOR_RE.search(question)
    if product_code:
        return product_code.group(1).upper()
    kcd = _KCD_ANCHOR_RE.search(question)
    if kcd:
        return kcd.group(1).replace(".", "").upper()
    return None


def _lock_exact_anchor(question: str, plan: PlannerOutput) -> PlannerOutput:
    anchor = _exact_anchor(question)
    if not anchor:
        return plan
    is_nct = anchor.startswith("NCT")
    is_product_code = _PRODUCT_CODE_ANCHOR_RE.search(question) is not None
    suffixes = {
        "mart": "내부 시장 데이터",
        "nedrug": "국내 허가 정보",
        "hira": "국내 급여 및 환자 통계",
        "openfda": "미국 허가 및 안전성",
        "clinicaltrials": "임상시험 상세",
        "web": "공식 최신 자료",
        "patent": "특허 공식 자료",
    }
    answer_sources: tuple[SourceName, ...] = (
        ("clinicaltrials",)
        if is_nct
        else ("nedrug",)
        if is_product_code
        else plan.answer_sources
    )
    return plan.model_copy(
        update={
            "answer_sources": answer_sources,
            "tool_queries": ToolQueries(
                **{source: (f"{anchor} {suffixes[source]}",) for source in SOURCE_NAMES}
            ),
            "needs_second_hop": is_nct or is_product_code,
        }
    )


def _lock_linked_anchor(
    anchor: str,
    linked: PlannerOutput,
    first_results: Sequence[SourceResult],
) -> PlannerOutput:
    entities = _canonical_anchor_entities(
        first_results,
        source="clinicaltrials" if anchor.startswith("NCT") else "nedrug",
    )
    subject = " ".join((anchor, *entities[:3]))
    suffixes = {
        "mart": "내부 시장 데이터",
        "nedrug": "국내 허가 정보",
        "hira": "국내 급여 정보",
        "openfda": "미국 허가 안전성",
        "clinicaltrials": "임상시험 상세",
        "web": "공식 최신 자료",
        "patent": "특허 공식 자료",
    }
    return linked.model_copy(
        update={
            "tool_queries": ToolQueries(
                **{source: (f"{subject} {suffixes[source]}",) for source in SOURCE_NAMES}
            )
        }
    )


def _canonical_anchor_entities(
    results: Sequence[SourceResult],
    *,
    source: SourceName = "clinicaltrials",
) -> tuple[str, ...]:
    found: list[str] = []

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                current = (*path, str(key).casefold())
                leaf = str(key).casefold()
                clinical_entity = leaf == "interventionname" or (
                    "interventions" in current and leaf == "name"
                )
                product_entity = leaf in {
                    "item_name",
                    "product_name",
                    "item_ingr_name",
                    "ingredient",
                    "entp_name",
                    "manufacturer",
                    "company",
                }
                if isinstance(item, str) and item.strip() and (
                    clinical_entity if source == "clinicaltrials" else product_entity
                ):
                    found.append(item.strip())
                walk(item, current)
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    for result in results:
        if result.source == source and result.status == "ok":
            walk(result.payload)
    return tuple(dict.fromkeys(found))


def _planner_messages(
    question: str,
    turns: Sequence[ConversationTurn],
    *,
    state: SessionState | None = None,
    observed_on: date | None = None,
) -> list[dict[str, str]]:
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
                "one useful query. Patent queries feed separate Korean MFDS, US Orange Book, and news lanes. "
                "For clinical trials, populate clinical_query_specs with searchable concepts rather than "
                "document filler phrases: ingredients, brands, intervention/condition search area, both/any "
                "matching, countries, statuses, and the corresponding source_queries. "
                "Populate requested_answer_shape.entities with every distinct entity resolved from the "
                "question and recent-turn context. When the interpretation names KCD codes or products, "
                "preserve the complete set in that structured field. "
                "Set answer_sources to the smallest source list that directly answers the user's question; "
                "these sources form the evidence quorum while all seven sources still run. "
                "Return JSON only, with no markdown. Set needs_second_hop only when first-hop entities are "
                "needed for one additional linking round. Schema: "
                + json.dumps(schema, ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "recent_turns": history,
                    "session_state": state.public_dict() if state else None,
                    "as_of_date_context": as_of_date_instruction(
                        observed_on or _current_kst_date()
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]


def _anchor_relative_years(
    question: str,
    plan: PlannerOutput,
    observed_on: date,
) -> PlannerOutput:
    match = _RELATIVE_YEAR_RE.search(question)
    if match is None:
        return plan
    count = max(1, min(20, int(match.group("count"))))
    canonical = f"{observed_on.year - count}년~{observed_on.year}년"
    relative = f"최근 {count}년"

    def normalize(value: str) -> str:
        normalized, replacements = _EXPLICIT_YEAR_RANGE_RE.subn(canonical, value)
        if replacements:
            return (
                normalized
                if _RELATIVE_YEAR_RE.search(normalized)
                else f"{normalized.rstrip()}({relative})"
            )
        normalized, replacements = _RELATIVE_YEAR_RE.subn(
            f"{canonical}({relative})",
            normalized,
        )
        if replacements:
            return normalized
        return f"{normalized.rstrip()} {canonical}({relative})"

    return plan.model_copy(
        update={
            "resolved_question": normalize(plan.resolved_question),
            "expanded_intents": tuple(normalize(value) for value in plan.expanded_intents),
            "tool_queries": ToolQueries(
                **{
                    source: tuple(normalize(query) for query in queries)
                    for source, queries in plan.tool_queries.items()
                }
            ),
            "linking_plan": normalize(plan.linking_plan),
        }
    )


def _parse_plan(raw: str) -> PlannerOutput:
    cleaned = _JSON_FENCE_RE.sub("", raw.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("planner output has no JSON object")
    return PlannerOutput.model_validate_json(cleaned[start : end + 1])


def _limit_first_wave_queries(plan: PlannerOutput) -> PlannerOutput:
    try:
        limit = int(os.environ.get("CHAT_V4_MAX_SOURCE_QUERIES", "1"))
    except ValueError:
        limit = 1
    entity_count = min(
        len(plan.requested_answer_shape.entities),
        configured_entity_limit(),
    )
    limit = max(1, min(max(limit, entity_count), SOURCE_CALL_LIMIT))
    requested_calls: dict[SourceName, int] = {}
    executed_calls: dict[SourceName, int] = {}
    omitted_queries: dict[SourceName, tuple[str, ...]] = {}
    limited_queries: dict[SourceName, tuple[str, ...]] = {}
    for source, queries in plan.tool_queries.items():
        unique = tuple(dict.fromkeys(queries))
        selected = unique if source == "clinicaltrials" else unique[:limit]
        requested_calls[source] = len(unique)
        executed_calls[source] = len(selected)
        if len(selected) < len(unique):
            omitted_queries[source] = unique[len(selected) :]
        limited_queries[source] = selected
    return plan.model_copy(
        update={
            "tool_queries": ToolQueries(**limited_queries),
            "query_scope": QueryScope(
                requested_calls=requested_calls,
                executed_calls=executed_calls,
                omitted_queries=omitted_queries,
            ),
        }
    )


def _attach_lossless_contracts(question: str, plan: PlannerOutput) -> PlannerOutput:
    grouped: dict[tuple[tuple[str, str], ...], tuple[str, ClinicalTrialConcept]] = {}
    supplied = plan.clinical_query_specs
    query_concepts: list[tuple[str, ClinicalTrialConcept]] = []
    raw_queries = plan.tool_queries.clinicaltrials
    for index, query in enumerate(raw_queries):
        if index < len(supplied):
            concept = supplied[index]
            if len(supplied) < len(raw_queries) and query not in concept.source_queries:
                concept = concept.model_copy(
                    update={
                        "source_queries": tuple(
                            dict.fromkeys((*concept.source_queries, query))
                        )
                    }
                )
        else:
            concept = concept_from_query(
                query,
                search_area=_clinical_search_area(question, query),
            )
        query_concepts.append((query, concept))
    for concept in supplied[len(raw_queries) :]:
        compiled = compile_clinical_query(concept)
        query_concepts.append(
            (
                concept.source_queries[0]
                if concept.source_queries
                else compiled.expression,
                concept,
            )
        )

    for query, concept in query_concepts:
        compiled = compile_clinical_query(concept)
        key = tuple(
            sorted((name, str(value)) for name, value in compiled.parameters.items())
        )
        if key not in grouped:
            grouped[key] = (query, concept)
            continue
        first_query, existing = grouped[key]
        grouped[key] = (
            first_query,
            existing.model_copy(
                update={
                    "source_queries": tuple(
                        dict.fromkeys((*existing.source_queries, *concept.source_queries))
                    )
                }
            ),
        )

    queries = tuple(value[0] for value in grouped.values())
    concepts = tuple(value[1] for value in grouped.values())
    deterministic_shape = _requested_answer_shape(question)
    supplied_shape = plan.requested_answer_shape
    merged_shape = supplied_shape.model_copy(
        update={
            "entities": tuple(
                dict.fromkeys((*supplied_shape.entities, *deterministic_shape.entities))
            ),
            "measure_or_attribute": tuple(
                dict.fromkeys(
                    (
                        *supplied_shape.measure_or_attribute,
                        *deterministic_shape.measure_or_attribute,
                    )
                )
            ),
            "time_horizon": supplied_shape.time_horizon or deterministic_shape.time_horizon,
            "granularity": supplied_shape.granularity or deterministic_shape.granularity,
            "period_from": supplied_shape.period_from or deterministic_shape.period_from,
            "period_to": supplied_shape.period_to or deterministic_shape.period_to,
        }
    )
    return plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(
                update={"clinicaltrials": queries or plan.tool_queries.clinicaltrials}
            ),
            "clinical_query_specs": concepts,
            "requested_answer_shape": merged_shape,
        }
    )


def _clinical_search_area(question: str, query: str) -> str:
    lowered = f"{question} {query}".casefold()
    if any(token in lowered for token in ("질환", "상병", "disease", "condition")):
        return "condition"
    return "intervention"


def _requested_answer_shape(question: str) -> RequestedAnswerShape:
    lowered = question.casefold()
    attributes: list[str] = []
    attribute_markers = (
        ("api_unit_price", ("api 단가", "원료의약품 단가", "원료 단가")),
        ("sales", ("매출",)),
        ("market_share", ("점유율", " ms ")),
        ("patient_count", ("환자수", "환자 수")),
        ("patent", ("특허",)),
        ("reimbursement", ("급여",)),
        ("active_clinical_trials", ("진행 중", "모집 중", "recruiting")),
        ("clinical_trials", ("임상", "clinical")),
    )
    for name, markers in attribute_markers:
        if any(marker in lowered for marker in markers):
            attributes.append(name)

    entities: list[str] = []
    if any(marker in lowered for marker in ("국내", "한국", "korea")):
        entities.append("country:KR")
    entities.extend(_NCT_ANCHOR_RE.findall(question.upper()))
    entities.extend(query_entity_candidates(question))
    subject_match = re.search(
        r"(?P<entity>[가-힣A-Za-z0-9_-]{2,40})\s*(?:의\s*)?"
        r"(?:API|원료의약품|원료|매출|점유율|특허|임상|급여)",
        question,
        re.IGNORECASE,
    )
    if subject_match and is_query_entity_candidate(subject_match.group("entity")):
        entities.append(subject_match.group("entity"))

    horizon_match = re.search(r"최근\s*\d{1,2}\s*년", question)
    range_match = _EXPLICIT_YEAR_RANGE_RE.search(question)
    horizon = horizon_match.group(0) if horizon_match else (
        range_match.group(0) if range_match else None
    )
    granularity = None
    if any(marker in lowered for marker in ("연도별", "연간", "년별")):
        granularity = "year"
    elif any(marker in lowered for marker in ("분기별", "분기")):
        granularity = "quarter"
    elif any(marker in lowered for marker in ("월별", "월간")):
        granularity = "month"

    period_from, period_to = _requested_period_bounds(question)
    return RequestedAnswerShape(
        entities=tuple(dict.fromkeys(entities)),
        measure_or_attribute=tuple(dict.fromkeys(attributes)),
        time_horizon=horizon,
        granularity=granularity,
        period_from=period_from,
        period_to=period_to,
    )


def _requested_period_bounds(question: str) -> tuple[str | None, str | None]:
    today = _current_kst_date()
    recent = _RELATIVE_YEAR_RE.search(question)
    if recent:
        months = max(1, min(120, int(recent.group("count")) * 12))
        end_index = today.year * 12 + today.month - 1
        start_index = end_index - months + 1
        return (
            f"{start_index // 12:04d}-{start_index % 12 + 1:02d}",
            f"{today.year:04d}-{today.month:02d}",
        )
    explicit_years = tuple(int(value) for value in re.findall(r"(?<!\d)(20\d{2})\s*년", question))
    if explicit_years:
        start_year, end_year = min(explicit_years), max(explicit_years)
        end_month = today.month if end_year == today.year else 12
        return f"{start_year:04d}-01", f"{end_year:04d}-{end_month:02d}"
    return None, None


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
        answer_sources=_fallback_answer_sources(question),
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


def _fallback_answer_sources(question: str) -> tuple[SourceName, ...]:
    lowered = question.casefold()
    if any(token in lowered for token in ("환자", "상병", "급여")):
        return ("hira",)
    if any(token in lowered for token in ("효능", "효과", "허가", "성분", "재심사")):
        return ("nedrug",)
    if "nct" in lowered or "임상" in lowered:
        return ("clinicaltrials",)
    if any(token in lowered for token in ("매출", "점유율", "순위", "성장", "경쟁")):
        return ("mart",)
    return ("web",)


def _direct_answer_sources(question: str) -> tuple[SourceName, ...]:
    lowered = question.casefold()
    if any(token in lowered for token in ("환자", "상병", "급여")):
        return ("hira",)
    if any(
        token in lowered
        for token in ("효능", "효과", "허가", "성분", "제네릭", "바이오시밀러", "재심사")
    ):
        return ("nedrug",)
    if any(token in lowered for token in ("nct", "임상", "신약 개발", "시험 디자인", "선정", "제외기준")):
        return ("clinicaltrials",)
    if "특허" in lowered:
        return ("patent",)
    if any(token in lowered for token in ("매출", "점유율", "순위", "성장", "경쟁", "시장", "요즘")):
        return ("mart",)
    if any(token in lowered for token in ("안전성", "부작용", "이상사례")):
        return ("openfda",)
    return ()


def _normalized_usage(usage: dict[str, object]) -> dict[str, int | None]:
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": _optional_int(usage.get("prompt_tokens")),
        "output_tokens": _optional_int(usage.get("completion_tokens")),
        "thinking_tokens": _optional_int(details.get("reasoning_tokens")),
    }


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None
