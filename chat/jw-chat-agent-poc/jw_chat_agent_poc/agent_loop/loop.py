from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
import json
import logging
import math
from typing import Any

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.bq_slots import requested_prescription_metric
from jw_chat_agent_poc.agent_loop.models import AgentDecision, AgentObservation, AgentTraceStep, ToolCallPlan, ToolPlanner
from jw_chat_agent_poc.agent_loop.parallel_execution import (
    TimedExecution,
    execute_tool_batch,
    planned_parallel_tool_names,
)
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner, HeuristicToolPlanner
from jw_chat_agent_poc.agent_loop.structured_planner import plan_structured_market_question
from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan
from jw_chat_agent_poc.agent_loop.external_tools import background_news_context_call
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade, ToolExecution
from jw_chat_agent_poc.orchestrator.answer_contract import CONTRACT_REQUIRED_TOOLS, answer_contract_backfill_tool_calls, evaluate_answer_contract
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_call_status
from jw_chat_agent_poc.orchestrator.answer_completeness import comparison_subjects, completeness_intent
from jw_chat_agent_poc.orchestrator.bq_enrichment import build_bq_analysis_call
from jw_chat_agent_poc.orchestrator.bq_runtime_guard import BQAnalysisValidationError, validate_bq_analysis_call
from jw_chat_agent_poc.orchestrator.narrative_intent import needs_market_series
from jw_chat_agent_poc.orchestrator.question_intent import allows_background_news_context
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.market_answer_contract import (
    market_ambiguity_message,
    market_membership_mismatch_message,
)
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.common.timing import add_stage, emit_completed_stage, new_timing, stage
from jw_chat_agent_poc.common.token_usage import record_token_usage
from jw_chat_agent_poc.common.periods import canonical_periods
from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace, qa_trace_started_at
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer

logger = logging.getLogger(__name__)

_PARALLEL_MARKET_TOOLS = frozenset(
    {
        "get_metric",
        "get_market_scope",
        "get_brand_sales",
        "get_brand_share",
        "get_brand_series",
        "get_top_brands",
    }
)
_DEEP_TOOL_GROUP_BY_NAME = {
    "get_metric": "시장",
    "get_market_scope": "시장",
    "get_brand_series": "시장",
    "get_top_brands": "시장",
    "search_news": "뉴스",
    "search_clinical": "임상",
    "search_drug_info": "허가",
    "get_disease_stats": "환자",
    "get_procedure_stats": "환자",
    "search_safety": "안전성",
    "search_patent": "특허",
    "csd_activity_trend": "영업 활동",
    "web_search": "웹",
}
_DEEP_TOOL_GROUP_ORDER = (
    "시장",
    "뉴스",
    "임상",
    "허가",
    "환자",
    "안전성",
    "특허",
    "영업 활동",
    "웹",
)


@dataclass(frozen=True, slots=True)
class ToolUseAgent:
    metrics: MetricsTool
    resolver: BrandResolver
    planner: ToolPlanner | None = None
    max_steps: int = 6
    current_month: Callable[[], str] | None = None
    news: DeepAnalysisNewsTool | None = None
    external: ExternalApiClient | None = None
    query_layer: StrategicQueryLayer | None = None
    progress_namespace: str = "standard"

    def answer(self, question: str) -> dict[str, Any]:
        timing = new_timing()
        planner = self.planner or GenosToolPlanner(fallback=HeuristicToolPlanner())
        with stage(timing, self._stage_name("agent_pre_resolve", "deep_research_prepare"), "brand and period grounding"):
            resolutions = _pre_resolutions(question, self.resolver)
            base_allowed_brands = tuple(item.canonical_brand for item in resolutions)
            market_by_brand = {
                item.canonical_brand: item.market_id
                for item in resolutions
                if item.market_id is not None
            }
            period_grounding = build_period_grounding(question, self.current_month)
        portfolio_call = _portfolio_decline_call(question, self.resolver, self.query_layer)
        if portfolio_call is not None:
            with stage(timing, "fact_assembly", "portfolio markdown fact set build"):
                markdown = MarkdownResponseBuilder().build(brand="JW 주요 브랜드", calls=[portfolio_call], sources=[portfolio_call.get("source") or "cache"])
            return {
                "question": question,
                "resolution": {"canonical_brand": "JW 주요 브랜드", "scope": "portfolio"},
                "decomposition": [{"intent": "portfolio_decline_analysis", "status": "ok", "max_steps": 0}],
                "router_diagnostics": {"mode": "agent_loop", "deterministic_execution": True, "scope": "portfolio"},
                "agent_trace": [],
                "agent_loop_metrics": {
                    "status": "ok",
                    "steps": 0,
                    "tool_calls": 1,
                    "selected_tools": ["portfolio_decline_analysis"],
                },
                "tool_calls": [portfolio_call],
                "answer": markdown.markdown,
                "markdown_response": markdown.to_dict(),
                "sources": [portfolio_call.get("source") or "cache"],
                "timing": timing,
            }
        mismatched = next(
            (item for item in resolutions if item.has_market_membership_mismatch),
            None,
        )
        if mismatched is not None:
            message = market_membership_mismatch_message(
                mismatched.canonical_brand,
                mismatched.requested_market_name or mismatched.requested_market_id or "요청 시장",
                mismatched.market_names or mismatched.market_ids,
            )
            return {
                "question": question,
                "resolution": asdict(mismatched),
                "decomposition": [{"intent": "market_membership_validation", "status": "unsupported", "max_steps": 0}],
                "router_diagnostics": {
                    "mode": "agent_loop",
                    "deterministic_execution": True,
                    "scope": "market_membership_mismatch",
                    "gate": "brand_market_membership",
                    "gate_reason": "explicit_market_outside_brand_memberships",
                },
                "agent_trace": [],
                "agent_loop_metrics": {"status": "unsupported", "steps": 0, "tool_calls": 0, "selected_tools": []},
                "tool_calls": [],
                "answer": message,
                "markdown_response": {"markdown": message, "fact_md": "", "data_md": ""},
                "sources": [],
                "timing": timing,
            }
        ambiguous = next((item for item in resolutions if item.requires_market_clarification), None)
        if ambiguous is not None:
            message = market_ambiguity_message(
                ambiguous.canonical_brand,
                ambiguous.market_names or ambiguous.market_ids,
            )
            return {
                "question": question,
                "resolution": asdict(ambiguous),
                "decomposition": [{"intent": "market_clarification", "status": "needs_clarification", "max_steps": 0}],
                "router_diagnostics": {"mode": "agent_loop", "deterministic_execution": True, "scope": "market_ambiguity"},
                "agent_trace": [],
                "agent_loop_metrics": {"status": "needs_clarification", "steps": 0, "tool_calls": 0, "selected_tools": []},
                "tool_calls": [],
                "answer": message,
                "markdown_response": {"markdown": message, "fact_md": "", "data_md": ""},
                "sources": [],
                "timing": timing,
            }
        observations: list[AgentObservation] = []
        calls: list[dict[str, Any]] = []
        trace: list[AgentTraceStep] = []
        seen: set[str] = set()
        notices: list[str] = []
        status = "ok"
        expanded_members_exposed = False
        deterministic_plan_hit = False
        deterministic_plan_kind: str | None = None
        bq_analysis_validation = "not_applicable"
        bq_missing_sources: tuple[str, ...] = ()
        llm_plan_calls = 0
        for step in range(1, self.max_steps + 1):
            allowed_brands = _step_allowed_brands(base_allowed_brands, tuple(observations))
            planner_allowed_brands = _planner_allowed_brands(
                base_allowed_brands,
                tuple(observations),
                expanded_members_exposed=expanded_members_exposed,
            )
            facade = AgentToolFacade(
                metrics=self.metrics,
                resolver=self.resolver,
                current_month=self.current_month,
                allowed_brands=allowed_brands,
                period_grounding=period_grounding,
                news=self.news,
                external=self.external,
                query_layer=self.query_layer,
                market_by_brand=market_by_brand,
            )
            with stage(timing, self._stage_name("market_snapshot", "deep_research_plan"), "tool catalog and market snapshot"):
                tool_schemas = facade.schemas(planner_allowed_brands)
            period_detail = ", ".join(period_grounding.pre_resolved_periods) or "latest"
            brand_detail = ", ".join(planner_allowed_brands) or "unresolved"
            bq_plan = (
                plan_bq_question(
                    question,
                    self.resolver,
                    period_grounding,
                    tool_schemas,
                    facade.available_sources(),
                )
                if self.planner is None and not observations
                else None
            )
            structured_plan = (
                plan_structured_market_question(
                    question,
                    self.resolver,
                    period_grounding,
                    tool_schemas,
                )
                if (
                    bq_plan is None
                    and not observations
                    and (self.planner is None or is_explicit_quarter_sales_question(question))
                )
                else None
            )
            if bq_plan is not None:
                with stage(timing, "deterministic_plan", f"브랜드={brand_detail}; 기간={period_detail}") as progress:
                    decision = bq_plan.decision
                    deterministic_plan_hit = True
                    deterministic_plan_kind = f"BQ:{bq_plan.contract.contract_id}"
                    bq_missing_sources = bq_plan.missing_sources
                    progress.summary = " -> ".join(call.name for call in decision.tool_calls)
            elif structured_plan is not None:
                with stage(timing, "deterministic_plan", f"브랜드={brand_detail}; 기간={period_detail}") as progress:
                    decision = structured_plan.decision
                    deterministic_plan_hit = True
                    deterministic_plan_kind = structured_plan.kind
                    progress.summary = " -> ".join(call.name for call in decision.tool_calls)
            else:
                with stage(timing, self._stage_name("llm_plan", "deep_research_plan"), f"브랜드={brand_detail}; 기간={period_detail}") as progress:
                    decision = planner.decide(
                        question,
                        tuple(observations),
                        tool_schemas,
                        planner_allowed_brands,
                        period_grounding.schema_periods,
                    )
                    llm_plan_calls += 1
                    _record_planner_token_usage(timing, planner)
                    progress.summary = " -> ".join(call.name for call in decision.tool_calls) or "답변 생성"
            if _has_market_members(tuple(observations)):
                expanded_members_exposed = True
            if not decision.tool_calls:
                trace.append(_trace_step(step, decision, ()))
                break
            batch: list[AgentObservation] = []
            duplicate = False
            is_bq_batch = bool(
                deterministic_plan_kind and deterministic_plan_kind.startswith("BQ:")
            )
            parallel_market_tools = (
                _PARALLEL_MARKET_TOOLS
                if self.progress_namespace == "deep" or structured_plan is not None
                else ()
            )
            deep_batch_detail = _deep_batch_progress_detail(
                decision.tool_calls,
                parallel_market_tools,
            )

            def record_tool_completion(timed_execution: TimedExecution[ToolExecution]) -> None:
                plan = timed_execution.plan
                execution = timed_execution.result
                detail = f"step={step}; mode={timed_execution.mode}"
                summary = _deep_tool_progress_summary(execution) if self.progress_namespace == "deep" else None
                emit_completed_stage(
                    timing,
                    f"tool:{plan.name}",
                    timed_execution.elapsed_ms,
                    detail,
                    summary=summary,
                )

            if is_bq_batch:
                detail = (
                    deep_batch_detail
                    if self.progress_namespace == "deep"
                    else f"step={step}; bounded independent support tools"
                )
                with stage(
                    timing,
                    self._stage_name("tool_batch", "deep_research_tool_batch"),
                    detail,
                ) as batch_progress:
                    execution_batch = execute_tool_batch(
                        decision.tool_calls,
                        lambda plan: _execute_grounded(facade, plan),
                        additional_parallel_tools=parallel_market_tools,
                        on_complete=record_tool_completion,
                    )
                    if self.progress_namespace == "deep":
                        batch_progress.summary = f"{deep_batch_detail} 완료"
            else:
                detail = (
                    deep_batch_detail
                    if self.progress_namespace == "deep"
                    else f"step={step}; independent support tools"
                )
                with stage(
                    timing,
                    self._stage_name("tool_batch", "deep_research_tool_batch"),
                    detail,
                ) as batch_progress:
                    execution_batch = execute_tool_batch(
                        decision.tool_calls,
                        lambda plan: _execute_grounded(facade, plan),
                        additional_parallel_tools=parallel_market_tools,
                        on_complete=record_tool_completion,
                    )
                    if self.progress_namespace == "deep":
                        batch_progress.summary = f"{deep_batch_detail} 완료"
            for timed_execution in execution_batch:
                plan = timed_execution.plan
                execution = timed_execution.result
                key = _fingerprint(ToolCallPlan(name=plan.name, arguments=execution.arguments, reason=plan.reason))
                if key in seen:
                    duplicate = True
                    status = "duplicate_stopped"
                    notices.append("반복 도구 호출을 감지해 agent loop를 중단하고 확인된 도구 결과만 표시했습니다.")
                    break
                seen.add(key)
                observation = AgentObservation(step, plan.name, execution.arguments, execution.status, execution.preview, execution.call)
                observations.append(observation)
                batch.append(observation)
                calls.append(execution.call)
            trace.append(_trace_step(step, decision, tuple(batch)))
            if duplicate:
                break
            if _explicit_period_metric_no_data(question, tuple(item.call for item in batch)):
                break
            if is_bq_batch:
                break
            if _observation_is_sufficient_for_final_answer(question, tuple(observations), tuple(batch)):
                break
        else:
            status = "budget_exceeded"
            notices.append("agent loop step 예산을 초과해 확인된 도구 결과만 표시했습니다.")
        terminal_no_data_call = _explicit_period_metric_no_data(question, tuple(calls))
        if terminal_no_data_call is not None:
            data = terminal_no_data_call.get("render_data")
            message = str(data.get("message") or terminal_no_data_call.get("summary_text") or "")
            brand = base_allowed_brands[0] if base_allowed_brands else _answer_brand(question, self.resolver)
            markdown = MarkdownResponseBuilder().no_data(message)
            sources = _sources(calls)
            return {
                "question": question,
                "resolution": {"canonical_brand": brand},
                "decomposition": [{"intent": "agent_loop", "status": "no_data", "max_steps": self.max_steps}],
                "router_diagnostics": {
                    "mode": "agent_loop",
                    "deterministic_execution": True,
                    "gate": "typed_unavailable",
                    "gate_reason": "explicit_period_no_data",
                },
                "agent_trace": [item.to_dict() for item in trace],
                "agent_loop_metrics": {
                    "status": "no_data",
                    "steps": len(trace),
                    "tool_calls": len(calls),
                    "deterministic_plan_hit": deterministic_plan_hit,
                    "deterministic_plan_kind": deterministic_plan_kind,
                    "llm_plan_calls": llm_plan_calls,
                    "selected_tools": list(dict.fromkeys(item.tool_name for item in observations)),
                },
                "tool_calls": calls,
                "answer": markdown.markdown,
                "markdown_response": markdown.to_dict(),
                "sources": sources or [str(terminal_no_data_call.get("source") or "none")],
                "timing": timing,
            }
        observed_brands = _step_allowed_brands(base_allowed_brands, tuple(observations))
        brand = observed_brands[0] if observed_brands else _answer_brand(question, self.resolver)
        with stage(timing, "strict_query_plan", "population-sensitive spec mapping"):
            strict_calls = _strict_query_calls(
                question,
                calls,
                observations,
                brand,
                self.metrics,
                self.resolver,
                self.current_month,
                period_grounding,
                self.news,
                self.external,
                self.query_layer,
            )
        comparison = _comparison_brand_grounding(question, self.resolver, base_allowed_brands)
        metric_brands = tuple(dict.fromkeys((*base_allowed_brands, *comparison.supported_brands)))
        if strict_calls is not None:
            calls = strict_calls if _only_unsupported(strict_calls) else _non_metric_support_calls(calls) + strict_calls
            with stage(timing, "answer_contract_preflight", "required fact backfill"):
                calls.extend(
                    _answer_contract_calls(
                        question,
                        calls,
                        observations,
                        brand,
                        metric_brands or (brand,),
                        self.metrics,
                        self.resolver,
                        self.current_month,
                        period_grounding,
                        self.news,
                        self.external,
                        self.query_layer,
                    )
                )
                calls.extend(
                    _answer_contract_required_calls(
                        question,
                        calls,
                        observations,
                        brand,
                        metric_brands or (brand,),
                        self.metrics,
                        self.resolver,
                        self.current_month,
                        period_grounding,
                        self.news,
                        self.external,
                        self.query_layer,
                    )
                )
        else:
            with stage(timing, "completion_queries", "deterministic metric backfill"):
                calls.extend(
                    _completion_calls(
                        question,
                        calls,
                        observations,
                        brand,
                        metric_brands or (brand,),
                        comparison.unsupported_terms,
                        self.metrics,
                        self.resolver,
                        self.current_month,
                        period_grounding,
                        self.news,
                        self.external,
                        self.query_layer,
                    )
                )
            with stage(timing, "answer_contract_preflight", "required fact backfill"):
                calls.extend(
                    _answer_contract_calls(
                        question,
                        calls,
                        observations,
                        brand,
                        metric_brands or (brand,),
                        self.metrics,
                        self.resolver,
                        self.current_month,
                        period_grounding,
                        self.news,
                        self.external,
                        self.query_layer,
                    )
                )
            with stage(timing, "compute", "deterministic deltas and comparisons"):
                calculation_started_at = qa_trace_started_at()
                calculation_calls = _calculation_calls(question, calls, brand)
                for calculation_call in calculation_calls:
                    attach_tool_qa_trace(
                        calculation_call,
                        started_at=calculation_started_at,
                        status="ok",
                        row_count=1,
                        cache_hit=False,
                    )
                calls.extend(calculation_calls)
        _mark_answer_scope(question, calls, brand)
        with stage(timing, "context_retrieval", "background issue material"):
            calls.extend(_background_context_calls(question, calls, brand, self.news))
        if deterministic_plan_kind and deterministic_plan_kind.startswith("BQ:"):
            with stage(timing, "bq_analysis", "deterministic cross-source calculations"):
                bq_started_at = qa_trace_started_at()
                analysis_call = None
                if bq_missing_sources:
                    status = "source_unavailable"
                    bq_analysis_validation = "SOURCE_UNAVAILABLE"
                    labels = ", ".join(_bq_source_label(source) for source in bq_missing_sources)
                    notices.append(f"요청한 분석에 필요한 출처({labels})를 현재 조회할 수 없습니다.")
                else:
                    analysis_call = build_bq_analysis_call(deterministic_plan_kind.removeprefix("BQ:"), calls)
                if not bq_missing_sources and analysis_call is None:
                    status = "verification_failed"
                    bq_analysis_validation = "MISSING_EVIDENCE"
                    notices.append("분석에 필요한 근거가 완결되지 않아 해당 해석은 표시하지 않았습니다.")
                elif analysis_call is not None:
                    try:
                        validate_bq_analysis_call(analysis_call)
                    except BQAnalysisValidationError as exc:
                        status = "verification_failed"
                        bq_analysis_validation = "VERIFICATION_FAIL"
                        notices.append("분석 근거 검증을 통과하지 못해 해당 해석은 표시하지 않았습니다.")
                        logger.warning(
                            "bq_analysis_validation_failed contract=%s reason=%s",
                            deterministic_plan_kind,
                            exc,
                        )
                    else:
                        bq_analysis_validation = "passed"
                        attach_tool_qa_trace(
                            analysis_call,
                            started_at=bq_started_at,
                            status="ok",
                            row_count=1,
                            cache_hit=False,
                        )
                        calls.append(analysis_call)
        sources = _sources(calls)
        selection = _tool_selection(question, calls)
        with stage(timing, self._stage_name("fact_assembly", "deep_research_evidence"), "markdown fact set build"):
            markdown = MarkdownResponseBuilder().build(brand=brand, calls=calls, sources=sources or ["cache"], notices=notices)
        return {
            "question": question,
            "resolution": {"canonical_brand": brand},
            "decomposition": [{"intent": "agent_loop", "status": status, "max_steps": self.max_steps}],
            "router_diagnostics": {"mode": "agent_loop", "deterministic_execution": True},
            "agent_trace": [item.to_dict() for item in trace],
            "agent_loop_metrics": {
                "status": status,
                "steps": len(trace),
                "tool_calls": len([call for call in calls if call.get("tool") != "agent_calculation"]),
                "deterministic_plan_hit": deterministic_plan_hit,
                "deterministic_plan_kind": deterministic_plan_kind,
                "llm_plan_calls": llm_plan_calls,
                "bq_analysis_validation": bq_analysis_validation,
                "bq_missing_sources": list(bq_missing_sources),
                "selected_tools": list(dict.fromkeys(item.tool_name for item in observations)),
                **selection,
            },
            "tool_calls": calls,
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": sources or ["cache"],
            "timing": timing,
        }

    def _stage_name(self, standard: str, deep: str) -> str:
        return deep if self.progress_namespace == "deep" else standard


def _bq_source_label(source: str) -> str:
    return {"iqvia_nsa": "IQVIA NSA", "ubist": "UBIST"}.get(source, source)


def _deep_tool_progress_summary(execution: ToolExecution) -> str:
    call = execution.call if isinstance(execution.call, dict) else {}
    status = str(call.get("status") or execution.status or "")
    if status in {"no_data", "unsupported", "inapplicable"}:
        return "확인된 결과 없음"
    count = _deep_evidence_count(call.get("render_data"))
    return f"{count}건 확인" if count is not None else "조회 완료"


def _deep_evidence_count(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ("items", "rows", "results", "events", "studies", "articles"):
        items = value.get(key)
        if isinstance(items, list):
            return len(items)
    nested_calls = value.get("calls")
    if not isinstance(nested_calls, list):
        return None
    counts = [
        count
        for item in nested_calls
        if isinstance(item, dict)
        for count in (_deep_evidence_count(item.get("render_data")),)
        if count is not None
    ]
    return sum(counts) if counts else None


def is_explicit_quarter_sales_question(question: str) -> bool:
    plan = strict_query_plan(question, "")
    if plan is None or len(plan.specs) != 1 or len(plan.metadata) != 1:
        return False
    if plan.metadata[0].get("contract_intent") != "quarter_metric":
        return False
    if plan.specs[0].get("metrics") != ["sales"]:
        return False
    return not any(token in question for token in ("비교", "각각", "추이", "변화", "증감", "대비", "차이"))


def _record_planner_token_usage(timing: dict[str, Any], planner: ToolPlanner) -> None:
    usage = getattr(planner, "last_token_usage", None)
    if isinstance(usage, dict):
        record_token_usage(timing, usage)


def _execute_grounded(facade: AgentToolFacade, plan: ToolCallPlan) -> ToolExecution:
    try:
        grounded_arguments = facade.ground_arguments(plan.name, plan.arguments)
    except (LookupError, TypeError, ValueError, UnsupportedBrandError):
        return facade.execute(plan.name, plan.arguments)
    return facade.execute(plan.name, grounded_arguments)


def _portfolio_decline_call(
    question: str,
    resolver: BrandResolver,
    query_layer: StrategicQueryLayer | None,
) -> dict[str, Any] | None:
    if query_layer is None or not is_portfolio_decline_question(question):
        return None
    brands = tuple(
        {
            "brand": item.canonical_brand,
            "market_id": item.market_id,
            "market_name": item.market_name,
        }
        for item in resolver.portfolio_brands()
    )
    return query_layer.portfolio_decline_analysis(brands)


def _trace_step(step: int, decision: AgentDecision, observations: tuple[AgentObservation, ...]) -> AgentTraceStep:
    return AgentTraceStep(step=step, decision={"tool_calls": [item.to_dict() for item in decision.tool_calls], "final_answer": decision.final_answer}, observations=tuple(item.to_dict() for item in observations))


def _fingerprint(plan: ToolCallPlan) -> str:
    return json.dumps({"name": plan.name, "arguments": dict(sorted(plan.arguments.items()))}, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class ComparisonBrandGrounding:
    supported_brands: tuple[str, ...]
    unsupported_terms: tuple[str, ...]


def _calculation_calls(question: str, calls: list[dict[str, Any]], anchor_brand: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "제일 큰" in question or "가장 큰" in question:
        call = _largest_competitor_call(anchor_brand, calls)
        if call is not None:
            out.append(call)
    if _asks_share_delta(question):
        call = _share_delta_call(calls)
        if call is not None:
            out.append(call)
    if _asks_sales_change(question):
        out.extend(_sales_delta_calls(calls))
    if _asks_market_vs_brand_comparison(question):
        call = _market_vs_brand_delta_call(question, calls, anchor_brand)
        if call is not None:
            out.append(call)
    if _asks_series_comparison(question):
        call = _brand_trend_comparison_call(calls, anchor_brand)
        if call is not None:
            out.append(call)
    if _needs_competitive_insight_signals(question):
        call = _competitive_insight_signals_call(calls)
        if call is not None:
            out.append(call)
    return out


def _mark_answer_scope(question: str, calls: list[dict[str, Any]], anchor_brand: str) -> None:
    """Mark simple brand-trend material so fact assembly does not widen into top-brand context."""

    scope = _answer_scope(question)
    if scope is None:
        return
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = _metric_data(call)
        if data.get("brand") != anchor_brand:
            continue
        if data.get("level") == "channel" and isinstance(data.get("level_segments"), list):
            continue
        if scope == "single_brand_trend" and data.get("metric") != "series":
            continue
        data["answer_scope"] = scope


def _answer_scope(question: str) -> str | None:
    if _single_brand_trend_question(question):
        return "single_brand_trend"
    if _single_brand_focus_question(question):
        return "single_brand_focus"
    return None


def _background_context_calls(
    question: str,
    calls: list[dict[str, Any]],
    anchor_brand: str,
    news: DeepAnalysisNewsTool | None,
) -> list[dict[str, Any]]:
    """Attach issue context for quantitative or judgment questions without requiring a news cue."""

    if news is None or not _needs_background_news_context(question, calls):
        return []
    if any(call.get("tool") == "deep_analysis_related_news" for call in calls):
        return []
    try:
        started_at = qa_trace_started_at()
        relevance_brands = () if _asks_change_driver_context(question) else _background_news_relevance_brands(calls, anchor_brand)
        call = background_news_context_call(news, anchor_brand, relevance_brands)
        attach_tool_qa_trace(call, started_at=started_at)
        return [call]
    except Exception:
        return []


def _background_news_relevance_brands(calls: list[dict[str, Any]], anchor_brand: str) -> tuple[str, ...]:
    """Reuse already-computed market-structure brands to constrain background news."""

    brands: list[str] = []
    for call in calls:
        data = _metric_data(call)
        for key in ("level_top5_trend_series", "level_segments"):
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("brand") or row.get("name") or "").strip()
                if name:
                    brands.append(name)
    unique = tuple(dict.fromkeys(brands))
    competitors = tuple(brand for brand in unique if brand != anchor_brand)
    return competitors or unique


def _needs_background_news_context(question: str, calls: list[dict[str, Any]]) -> bool:
    if any(token in question for token in ("뉴스", "이슈", "소식", "기사")):
        return False
    if not allows_background_news_context(question):
        return False
    if _asks_change_driver_context(question):
        return True
    return any(_is_material_metric_call(call) for call in calls)


def _asks_change_driver_context(question: str) -> bool:
    return any(token in question for token in ("변화 요인", "변화요인", "Market expansion", "External", "Internal", "보건 정책", "Line extension"))


def _is_material_metric_call(call: dict[str, Any]) -> bool:
    if call.get("tool") == "agent_calculation":
        return True
    if call.get("tool") != "get_brand_metric":
        return False
    data = call.get("render_data")
    if not isinstance(data, dict):
        return False
    if data.get("status") == "unsupported":
        return False
    return any(
        data.get(key) not in (None, "", [])
        for key in (
            "sales_krw",
            "ms_recent_pct",
            "brand_value_series_10pt",
            "level_segments",
            "level_top5_trend_series",
            "market_size_series",
            "growth_pct",
        )
    )


def _completion_calls(
    question: str,
    calls: list[dict[str, Any]],
    observations: list[AgentObservation],
    anchor_brand: str,
    metric_brands: tuple[str, ...],
    unsupported_comparison_terms: tuple[str, ...],
    metrics: MetricsTool,
    resolver: BrandResolver,
    current_month: Callable[[], str] | None,
    period_grounding,
    news: DeepAnalysisNewsTool | None,
    external: ExternalApiClient | None,
    query_layer: StrategicQueryLayer | None,
) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    facade: AgentToolFacade | None = None
    if _asks_share_delta(question):
        facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
        for metric_brand in metric_brands:
            for period_arg, period_display in _share_delta_periods(calls, period_grounding):
                if _has_market_share_metric(calls, metric_brand, period_display) or _has_market_share_metric(completed, metric_brand, period_display):
                    continue
                execution = facade.execute("get_metric", {"brand": metric_brand, "measure": "market_share", "period": period_arg})
                call = dict(execution.call)
                data = call.setdefault("render_data", {})
                if isinstance(data, dict):
                    data["completion_reason"] = "share_delta_requires_period_metrics"
                completed.append(call)
    if _asks_sales_change(question):
        if facade is None:
            facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
        for metric_brand in metric_brands:
            if _has_sales_series(calls, metric_brand) or _has_sales_series(completed, metric_brand):
                continue
            execution = facade.execute("get_metric", {"brand": metric_brand, "measure": "series", "period": "latest"})
            call = dict(execution.call)
            data = call.setdefault("render_data", {})
            if isinstance(data, dict):
                data["completion_reason"] = "sales_change_requires_series"
            completed.append(call)
        for term in unsupported_comparison_terms:
            member_metric = _market_member_segment_metric_call(anchor_brand, term, metrics, query_layer)
            completed.append(member_metric if member_metric is not None else _unsupported_comparison_metric_call(term))
    if _asks_patient_sales_context(question):
        if facade is None:
            facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
        for metric_brand in metric_brands:
            if _has_sales_series(calls, metric_brand) or _has_sales_series(completed, metric_brand):
                continue
            execution = facade.execute("get_metric", {"brand": metric_brand, "measure": "series", "period": "latest"})
            call = dict(execution.call)
            data = call.setdefault("render_data", {})
            if isinstance(data, dict):
                data["completion_reason"] = "patient_sales_requires_series"
            completed.append(call)
    if _asks_series_comparison(question):
        if facade is None:
            facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
        for metric_brand in metric_brands:
            if _has_sales_series(calls, metric_brand) or _has_sales_series(completed, metric_brand):
                continue
            execution = facade.execute("get_metric", {"brand": metric_brand, "measure": "series", "period": "latest"})
            call = dict(execution.call)
            data = call.setdefault("render_data", {})
            if isinstance(data, dict):
                data["completion_reason"] = "comparison_trend_requires_series"
            completed.append(call)
        for term in unsupported_comparison_terms:
            if _has_sales_series(calls, term) or _has_sales_series(completed, term):
                continue
            member_metric = _market_member_segment_metric_call(anchor_brand, term, metrics, query_layer)
            completed.append(member_metric if member_metric is not None else _unsupported_comparison_metric_call(term))
    if _asks_issue_context_with_quant_link(question) and not _has_brand_metric_context(calls + completed, anchor_brand):
        if facade is None:
            facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
        execution = facade.execute("get_metric", {"brand": anchor_brand, "measure": "series", "period": "latest"})
        call = dict(execution.call)
        data = call.setdefault("render_data", {})
        if isinstance(data, dict):
            data["completion_reason"] = "issue_question_requires_brand_metric_context"
            data["answer_scope"] = "single_brand_trend"
        completed.append(call)
    if not ("제일 큰" in question or "가장 큰" in question):
        return completed
    members = _member_brands(calls)
    if not members:
        return completed
    existing = {str(_metric_data(call).get("brand")) for call in calls if call.get("tool") == "get_brand_metric"}
    missing = tuple(brand for brand in members if brand not in existing)
    if not missing:
        return completed
    if facade is None:
        facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
    for brand in missing:
        execution = facade.execute("get_metric", {"brand": brand, "measure": "sales", "period": "previous_year"})
        call = dict(execution.call)
        data = call.setdefault("render_data", {})
        if isinstance(data, dict):
            data["completion_reason"] = "largest_competitor_requires_member_metric"
        completed.append(call)
    return completed


def _strict_query_calls(
    question: str,
    calls: list[dict[str, Any]],
    observations: list[AgentObservation],
    brand: str,
    metrics: MetricsTool,
    resolver: BrandResolver,
    current_month: Callable[[], str] | None,
    period_grounding,
    news: DeepAnalysisNewsTool | None,
    external: ExternalApiClient | None,
    query_layer: StrategicQueryLayer | None,
) -> list[dict[str, Any]] | None:
    plan = strict_query_plan(question, brand)
    if plan is None:
        return None
    if plan.unsupported_message:
        return [_unsupported_population_call(plan.unsupported_message)]
    facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, (brand,), observations)
    strict_calls: list[dict[str, Any]] = []
    for index, spec in enumerate(plan.specs):
        execution = facade.execute("query", {"brand": brand, "spec": json.dumps(spec, ensure_ascii=False)})
        call = execution.call
        if index < len(plan.metadata):
            call = _with_strict_query_metadata(call, plan.metadata[index])
        strict_calls.append(call)
    if plan.needs_top_competitor_specialty:
        strict_calls.extend(_top_competitor_specialty_calls(facade, brand))
    if plan.needs_company_molecule:
        strict_calls.extend(_company_molecule_calls(facade, brand, strict_calls))
    return strict_calls or [_unsupported_population_call("요청한 모집단 query spec을 생성하지 못했습니다.")]


def _with_strict_query_metadata(call: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
    if not metadata:
        return call
    enriched = dict(call)
    data = enriched.get("render_data")
    if isinstance(data, dict):
        enriched["render_data"] = {**data, **metadata}
    return enriched


def _non_metric_support_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocked = {"get_brand_metric", "get_market_landscape", "agent_calculation", "unsupported_metric", "query_failed"}
    return [call for call in calls if str(call.get("tool") or "") not in blocked]


def _only_unsupported(calls: list[dict[str, Any]]) -> bool:
    return bool(calls) and all(call.get("tool") == "unsupported_metric" for call in calls)


def _top_competitor_specialty_calls(facade: AgentToolFacade, brand: str) -> list[dict[str, Any]]:
    top = facade.execute("get_top_brands", {"brand": brand, "limit": "4"}).call
    out = [top]
    competitors = _competitors_from_segments(_metric_data(top).get("level_segments"), brand, 3)
    for competitor in competitors:
        spec = {
            "source": "ubist",
            "view": "market_landscape",
            "dimensions": ["specialty"],
            "group_by": ["specialty"],
            "metrics": ["sales"],
            "filters": {"brand": competitor},
            "limit": 3,
        }
        out.append(facade.execute("query", {"brand": brand, "spec": json.dumps(spec, ensure_ascii=False)}).call)
    return out


def _company_molecule_calls(facade: AgentToolFacade, brand: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    companies = _company_names(calls, 3)
    out: list[dict[str, Any]] = []
    for company in companies:
        spec = {
            "source": "ubist",
            "view": "market_landscape",
            "dimensions": ["molecule"],
            "group_by": ["molecule"],
            "metrics": ["sales"],
            "filters": {"company": company},
            "limit": 1,
        }
        out.append(facade.execute("query", {"brand": brand, "spec": json.dumps(spec, ensure_ascii=False)}).call)
    return out


def _competitors_from_segments(segments: Any, brand: str, limit: int) -> tuple[str, ...]:
    names: list[str] = []
    if isinstance(segments, list):
        for item in segments:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("brand") or "")
            if name and name != brand:
                names.append(name)
    return tuple(names[:limit])


def _company_names(calls: list[dict[str, Any]], limit: int) -> tuple[str, ...]:
    names: list[str] = []
    for call in calls:
        data = _metric_data(call)
        if data.get("level") != "company":
            continue
        segments = data.get("level_segments")
        if not isinstance(segments, list):
            continue
        for item in segments:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
    return tuple(dict.fromkeys(names))[:limit]


def _unsupported_population_call(message: str) -> dict[str, Any]:
    return {
        "source": "UBIST",
        "tool": "unsupported_metric",
        "summary_text": message,
        "render_data": {"status": "unsupported", "message": message, "metric": "population_query"},
    }


def _completion_facade(
    metrics: MetricsTool,
    resolver: BrandResolver,
    current_month: Callable[[], str] | None,
    period_grounding,
    news: DeepAnalysisNewsTool | None,
    external: ExternalApiClient | None,
    query_layer: StrategicQueryLayer | None,
    metric_brands: tuple[str, ...],
    observations: list[AgentObservation],
) -> AgentToolFacade:
    allowed_brands = _step_allowed_brands(metric_brands, tuple(observations))
    market_by_brand = _observed_market_by_brand(observations)
    return AgentToolFacade(
        metrics=metrics,
        resolver=resolver,
        current_month=current_month,
        allowed_brands=allowed_brands,
        period_grounding=period_grounding,
        news=news,
        external=external,
        query_layer=query_layer,
        market_by_brand=market_by_brand,
    )


def _observed_market_by_brand(observations: list[AgentObservation]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for observation in observations:
        data = observation.call.get("render_data")
        if not isinstance(data, dict):
            continue
        brand = str(data.get("brand") or observation.arguments.get("brand") or "")
        query_spec = data.get("query_spec")
        market = str(data.get("market_id") or "")
        if not market and isinstance(query_spec, dict):
            market = str(query_spec.get("market_id") or query_spec.get("market") or "")
        if not market and observation.tool_name in {"get_market_scope", "get_market_landscape"}:
            market = str(data.get("market") or "")
        if brand and market:
            selected[brand] = market
    return selected


def _answer_contract_calls(
    question: str,
    calls: list[dict[str, Any]],
    observations: list[AgentObservation],
    brand: str,
    metric_brands: tuple[str, ...],
    metrics: MetricsTool,
    resolver: BrandResolver,
    current_month: Callable[[], str] | None,
    period_grounding,
    news: DeepAnalysisNewsTool | None,
    external: ExternalApiClient | None,
    query_layer: StrategicQueryLayer | None,
) -> list[dict[str, Any]]:
    plans = answer_contract_backfill_tool_calls(question, brand, calls)
    compare_brands = _comparison_contract_brands(question, resolver, metric_brands)
    if not plans and not compare_brands:
        return []
    allowed_brands = tuple(dict.fromkeys((*metric_brands, *compare_brands)))
    facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, allowed_brands, observations)
    completed: list[dict[str, Any]] = []
    for plan in plans:
        execution = _execute_grounded(facade, plan)
        call = dict(execution.call)
        data = call.setdefault("render_data", {})
        if isinstance(data, dict):
            data["completion_reason"] = "answer_contract_requires_ranking_facts"
        completed.append(call)
    for compare_brand in compare_brands:
        if _has_sales_series(calls + completed, compare_brand):
            continue
        execution = facade.execute("get_metric", {"brand": compare_brand, "measure": "series", "period": "latest"})
        call = dict(execution.call)
        data = call.setdefault("render_data", {})
        if isinstance(data, dict):
            data["completion_reason"] = "brand_compare_requires_each_series"
        completed.append(call)
    return completed


def _comparison_contract_brands(
    question: str,
    resolver: BrandResolver,
    metric_brands: tuple[str, ...],
) -> tuple[str, ...]:
    if completeness_intent(question) != "brand_compare":
        return ()
    brands = list(metric_brands)
    for subject in comparison_subjects(question):
        try:
            canonical = resolver.resolve(subject, allow_default=False).canonical_brand
        except UnsupportedBrandError:
            continue
        brands.append(canonical)
    return tuple(dict.fromkeys(brands))


def _answer_contract_required_calls(
    question: str,
    calls: list[dict[str, Any]],
    observations: list[AgentObservation],
    brand: str,
    metric_brands: tuple[str, ...],
    metrics: MetricsTool,
    resolver: BrandResolver,
    current_month: Callable[[], str] | None,
    period_grounding,
    news: DeepAnalysisNewsTool | None,
    external: ExternalApiClient | None,
    query_layer: StrategicQueryLayer | None,
) -> list[dict[str, Any]]:
    required_tools = _contract_required_tools(question)
    if not required_tools:
        return []
    contract_status = evaluate_answer_contract(question, "", None)
    structural_contract = str(contract_status.get("structural_contract") or "")
    if _has_unsupported_metric(calls) and structural_contract != "change_drivers":
        return []
    existing = _public_contract_tools(calls)
    plans: list[ToolCallPlan] = []
    seen_plans: set[str] = set()
    for required_tool in required_tools:
        if required_tool in existing:
            continue
        plan = _required_contract_plan(required_tool, question, brand)
        if plan is None:
            continue
        key = _fingerprint(plan)
        if key in seen_plans:
            continue
        seen_plans.add(key)
        plans.append(plan)
    if not plans:
        return []
    facade = _completion_facade(metrics, resolver, current_month, period_grounding, news, external, query_layer, metric_brands, observations)
    completed: list[dict[str, Any]] = []
    for plan in plans:
        if plan.name == "search_news" and not _asks_issue_context_with_quant_link(question) and news is not None:
            call = background_news_context_call(news, brand)
        else:
            execution = _execute_grounded(facade, plan)
            call = dict(execution.call)
        data = call.setdefault("render_data", {})
        if isinstance(data, dict):
            data["completion_reason"] = f"contract_required_tool:{plan.reason}"
        completed.append(call)
    return completed


def _contract_required_tools(question: str) -> tuple[str, ...]:
    status = evaluate_answer_contract(question, "", None)
    structural = status.get("structural_contract")
    if isinstance(structural, str) and structural:
        return CONTRACT_REQUIRED_TOOLS.get(structural, ())
    intent = status.get("intent")
    if isinstance(intent, str) and intent:
        return CONTRACT_REQUIRED_TOOLS.get(intent, ())
    return ()


def _required_contract_plan(required_tool: str, question: str, brand: str) -> ToolCallPlan | None:
    prescription_metric = requested_prescription_metric(question)
    if required_tool == "get_brand_metric":
        requested_periods = canonical_periods(question)
        return ToolCallPlan(
            name="get_metric",
            arguments={
                "brand": brand,
                "measure": prescription_metric or "sales",
                "period": requested_periods[0] if requested_periods else "latest",
            },
            reason=required_tool,
        )
    if required_tool == "market_scope":
        if prescription_metric == "prescription_volume":
            return None
        return ToolCallPlan(
            name="get_market_scope",
            arguments={"brand": brand, "view": "market_landscape"},
            reason=required_tool,
        )
    if required_tool == "search_news":
        return ToolCallPlan(
            name="search_news",
            arguments={"brand": brand, "query": question},
            reason=required_tool,
        )
    if required_tool in {"search_patent", "mfds_patent"}:
        return ToolCallPlan(name="search_patent", arguments={"brand": brand}, reason=required_tool)
    if required_tool == "mfds_permission_search":
        return ToolCallPlan(name="search_drug_info", arguments={"brand": brand}, reason=required_tool)
    if required_tool == "csd_activity_trend":
        return ToolCallPlan(name="csd_activity_trend", arguments={"brand": brand}, reason=required_tool)
    return None


def _public_contract_tools(calls: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for call in calls:
        tool = str(call.get("tool") or "")
        data = call.get("render_data")
        if tool == "get_brand_metric" and isinstance(data, dict) and data.get("metric") == "query_spec":
            names.add("query_spec")
            continue
        if tool == "get_market_landscape":
            names.add("market_scope")
            continue
        if tool == "deep_analysis_related_news" and isinstance(data, dict):
            if _is_public_news_search_contract_tool(data):
                names.add("search_news")
            continue
        names.add(tool)
        if isinstance(data, dict):
            nested = data.get("calls")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict) and item.get("tool"):
                        names.add(str(item["tool"]))
    return names


def _is_public_news_search_contract_tool(data: dict[str, Any]) -> bool:
    facade_tool = data.get("facade_tool")
    if facade_tool in {"search_news", "background_news_context"}:
        return True
    if data.get("context_role") == "background_insight":
        return False
    items = data.get("items")
    return isinstance(items, list) and bool(items)


def _has_unsupported_metric(calls: list[dict[str, Any]]) -> bool:
    return any(call.get("tool") == "unsupported_metric" for call in calls)


def _has_sales_series(calls: list[dict[str, Any]], brand: str | None = None) -> bool:
    for call in calls:
        data = _metric_data(call)
        if brand is not None and data.get("brand") != brand:
            continue
        series = data.get("brand_value_series_10pt")
        if isinstance(series, list) and len(series) >= 2:
            return True
    return False


def _has_market_share_metric(calls: list[dict[str, Any]], brand: str, period: str) -> bool:
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = _metric_data(call)
        if data.get("brand") == brand and data.get("period") == period and isinstance(data.get("ms_recent_pct"), int | float):
            return True
    return False


def _asks_issue_context_with_quant_link(question: str) -> bool:
    return any(token in question for token in ("뉴스", "이슈", "소식", "기사"))


def _has_brand_metric_context(calls: list[dict[str, Any]], brand: str) -> bool:
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = _metric_data(call)
        if data.get("brand") != brand or data.get("status") == "unsupported":
            continue
        if any(
            data.get(key) not in (None, "", [])
            for key in (
                "sales_krw",
                "sales_억원",
                "ms_recent_pct",
                "market_share",
                "rank",
                "brand_value_series_10pt",
            )
        ):
            return True
    return False


def _member_brands(calls: list[dict[str, Any]]) -> tuple[str, ...]:
    brands: list[str] = []
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        members = data.get("member_brands")
        if isinstance(members, tuple | list):
            brands.extend(str(member) for member in members)
    return tuple(dict.fromkeys(brands))


def _largest_competitor_call(anchor_brand: str, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [_metric_data(call) for call in calls if call.get("tool") == "get_brand_metric"]
    competitors = [row for row in rows if row.get("brand") != anchor_brand and isinstance(row.get("sales_krw"), int | float)]
    if not competitors:
        return None
    winner = max(competitors, key=lambda item: float(item["sales_krw"]))
    sales = float(winner["sales_krw"])
    brand = str(winner.get("brand") or "경쟁 브랜드")
    period = str(winner.get("period") or "")
    label = f"작년({period})" if period == "2025" else period
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": f"같은 시장에서 {label} 제일 큰 경쟁사는 {brand}이며 매출은 {sales / 100_000_000:,.2f}억원입니다.",
        "render_data": {"brand": brand, "metric": "largest_competitor_sales", "period": period, "sales_krw": sales, "sales_억원": round(sales / 100_000_000, 2), "calculation": "max competitor sales"},
    }


def _share_delta_call(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = _share_delta_metric_rows(calls)
    if len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda item: str(item.get("period") or ""))
    start_period = _relative_start_period(calls)
    if start_period:
        start = next((row for row in ordered if row.get("period") == start_period), ordered[0])
        end = ordered[-1]
    else:
        start, end = ordered[0], ordered[-1]
    delta = round(float(end["ms_recent_pct"]) - float(start["ms_recent_pct"]), 4)
    brand = str(end.get("brand") or start.get("brand") or "브랜드")
    period = f"{start.get('period')}→{end.get('period')}"
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": f"{brand} 3달전 대비 점유율이 {period} 기준 {delta:.2f}%p 변했습니다.",
        "render_data": {"brand": brand, "metric": "market_share_delta", "period": period, "from_ms_pct": start.get("ms_recent_pct"), "to_ms_pct": end.get("ms_recent_pct"), "ms_delta_pct": delta},
    }


def _share_delta_metric_rows(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        _metric_data(call)
        for call in calls
        if call.get("tool") == "get_brand_metric" and isinstance(_metric_data(call).get("ms_recent_pct"), int | float)
    ]
    if len(rows) >= 2:
        return rows
    series_rows: list[dict[str, Any]] = []
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = _metric_data(call)
        series = data.get("brand_value_series_10pt")
        if not isinstance(series, list):
            continue
        brand = str(data.get("brand") or "")
        for item in series:
            if not isinstance(item, dict) or not isinstance(item.get("ms_pct"), int | float):
                continue
            period = str(item.get("period") or "")
            if not _is_month_period(period):
                continue
            series_rows.append({"brand": brand, "period": period, "ms_recent_pct": item.get("ms_pct")})
    return series_rows


def _relative_start_period(calls: list[dict[str, Any]]) -> str:
    for call in calls:
        if call.get("tool") != "resolve_relative_date":
            continue
        period = str(_metric_data(call).get("period") or "")
        if _is_month_period(period):
            return period
    return ""


def _asks_sales_change(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("변화", "증감", "추이", "하락", "떨어", "감소", "줄"))


def _asks_share_delta(question: str) -> bool:
    return "점유율" in question and "대비" in question


def _asks_series_comparison(question: str) -> bool:
    if _asks_sales_change(question):
        return True
    if "점유율" in question and any(token in question for token in ("변화", "추이", "비교", "오르는", "동안")):
        return True
    return any(token in question for token in ("경쟁 구도", "위협"))


def _asks_patient_sales_context(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("환자", "환자수", "환자 수", "질병", "질환", "HIRA", "hira"))


def _single_brand_trend_question(question: str) -> bool:
    if not needs_market_series(question):
        return False
    widening_tokens = ("경쟁", "구도", "상위", "위협", "시장 영향", "시장 탓", "시장 문제", "고유", "아토젯", "비교", "같이", "랑")
    return not any(token in question for token in widening_tokens)


def _single_brand_focus_question(question: str) -> bool:
    if "매출" not in question and "점유율" not in question and "순위" not in question:
        return False
    if any(token in question for token in ("질병", "환자수", "환자 수", "HIRA", "hira")):
        widening_tokens = ("경쟁", "구도", "상위", "위협", "시장 영향", "시장 탓", "시장 문제", "비교")
        return not any(token in question for token in widening_tokens)
    widening_tokens = (
        "경쟁",
        "구도",
        "상위",
        "위협",
        "시장 영향",
        "시장 탓",
        "시장 문제",
        "고유",
        "비교",
        "추이",
        "변화",
        "증감",
        "하락",
        "감소",
        "줄",
        "아토젯",
        "같이",
        "랑",
    )
    return not any(token in question for token in widening_tokens)


def _needs_competitive_insight_signals(question: str) -> bool:
    return any(token in question for token in ("경쟁", "구도", "상위", "위협", "재편", "점유율 변화 비교", "브랜드 뭐"))


def _asks_market_vs_brand_comparison(question: str) -> bool:
    return (
        "시장" in question
        and "매출" in question
        and any(token in question for token in ("하락", "떨어", "고유", "영향", "문제", "탓"))
    )


def _share_delta_periods(calls: list[dict[str, Any]], period_grounding) -> tuple[tuple[str, str], ...]:
    starts = [period for period in period_grounding.pre_resolved_periods if _is_month_period(period)]
    for call in calls:
        data = _metric_data(call)
        period = str(data.get("period") or "")
        if call.get("tool") == "resolve_relative_date" and _is_month_period(period):
            starts.append(period)
    starts = list(dict.fromkeys(starts))
    if not starts:
        return ()
    return tuple((period, period) for period in starts[:1]) + (("latest", period_grounding.latest_period),)


def _is_month_period(period: str) -> bool:
    return len(period) == 7 and period[:4].isdigit() and period[4] == "-" and period[5:].isdigit()


def _sales_delta_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    grouped = _sales_series_points_by_brand(calls)
    common_periods = _common_recent_sales_period_pair(grouped)
    if common_periods is not None:
        start_period, end_period = common_periods
        for brand, points in grouped.items():
            call = _sales_delta_call_for_brand_periods(brand, dict(points), start_period, end_period)
            if call is not None:
                out.append(call)
        return out
    if len(grouped) > 1:
        return out
    for brand, points in grouped.items():
        call = _sales_delta_call_for_brand(brand, points)
        if call is not None:
            out.append(call)
    return out


def _sales_delta_call(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    grouped = _sales_series_points_by_brand(calls)
    if not grouped:
        return None
    brand, points = next(iter(grouped.items()))
    return _sales_delta_call_for_brand(brand, points)


def _sales_delta_call_for_brand(brand: str, points: list[tuple[str, float]]) -> dict[str, Any] | None:
    if len(points) < 2:
        return None
    start, end = points[-2], points[-1]
    return _sales_delta_call_for_brand_periods(brand, dict(points), start[0], end[0])


def _sales_delta_call_for_brand_periods(brand: str, points: dict[str, float], start_period: str, end_period: str) -> dict[str, Any] | None:
    start_value = points.get(start_period)
    end_value = points.get(end_period)
    if start_value is None or end_value is None:
        return None
    if start_value == 0:
        return None
    delta_krw = end_value - start_value
    delta_pct = round((delta_krw / start_value) * 100, 4)
    period = f"{start_period}→{end_period}"
    delta_eok = round(delta_krw / 100_000_000, 2)
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": f"{brand} 매출 변화는 {period} 기준 {delta_eok:,.2f}억원({delta_pct:.2f}%)입니다.",
        "render_data": {
            "brand": brand,
            "metric": "sales_delta",
            "period": period,
            "from_sales_krw": start_value,
            "to_sales_krw": end_value,
            "sales_delta_krw": delta_krw,
            "sales_delta_억원": delta_eok,
            "sales_delta_pct": delta_pct,
        },
    }


def _sales_series_points_by_brand(calls: list[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    grouped: dict[str, dict[str, float]] = {}
    for call in calls:
        data = _metric_data(call)
        series = data.get("brand_value_series_10pt")
        if not isinstance(series, list):
            continue
        brand = str(data.get("brand") or "브랜드")
        for item in series:
            if not isinstance(item, dict):
                continue
            period = item.get("period")
            value = item.get("value_krw")
            if isinstance(period, str) and isinstance(value, int | float):
                grouped.setdefault(brand, {})[period] = float(value)
    return {brand: sorted(points.items(), key=lambda item: item[0]) for brand, points in grouped.items()}


def _common_recent_sales_period_pair(grouped: dict[str, list[tuple[str, float]]]) -> tuple[str, str] | None:
    period_sets = [set(period for period, _value in points) for points in grouped.values() if len(points) >= 2]
    if not period_sets:
        return None
    common_periods = set.intersection(*period_sets)
    if len(common_periods) < 2:
        return None
    recent = sorted(common_periods)[-2:]
    return recent[0], recent[1]


def _market_vs_brand_delta_call(question: str, calls: list[dict[str, Any]], anchor_brand: str) -> dict[str, Any] | None:
    data = next((row for row in (_metric_data(call) for call in calls) if row.get("brand") == anchor_brand and isinstance(row.get("brand_value_series_10pt"), list)), None)
    if data is None:
        return None
    brand_points = _value_points(data.get("brand_value_series_10pt"))
    market_points = _value_points(data.get("market_size_series"))
    if len(brand_points) < 2 or len(market_points) < 2:
        return None
    target_period = _target_month_period(question, brand_points, month="02")
    if not target_period:
        return None
    start_period = _previous_month(target_period)
    if start_period not in brand_points or start_period not in market_points or target_period not in market_points:
        return None
    brand_start = brand_points[start_period]
    brand_end = brand_points[target_period]
    market_start = market_points[start_period]
    market_end = market_points[target_period]
    brand_delta_krw = brand_end - brand_start
    market_delta_krw = market_end - market_start
    brand_delta_pct = _pct_change(brand_start, brand_end)
    market_delta_pct = _pct_change(market_start, market_end)
    gap = round(brand_delta_pct - market_delta_pct, 4)
    relation = _market_vs_brand_relation(brand_delta_pct, market_delta_pct, gap)
    period = f"{start_period}→{target_period}"
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": (
            f"{anchor_brand} {period} 매출 변화율은 {brand_delta_pct:.2f}%, "
            f"시장 변화율은 {market_delta_pct:.2f}%입니다. 변화율 격차를 근거로 배경 요인을 해석합니다."
        ),
        "render_data": {
            "brand": anchor_brand,
            "metric": "market_vs_brand_delta",
            "period": period,
            "from_period": start_period,
            "to_period": target_period,
            "brand_from_sales_krw": brand_start,
            "brand_to_sales_krw": brand_end,
            "brand_sales_delta_krw": brand_delta_krw,
            "brand_sales_delta_억원": round(brand_delta_krw / 100_000_000, 2),
            "brand_delta_pct": brand_delta_pct,
            "market_from_sales_krw": market_start,
            "market_to_sales_krw": market_end,
            "market_sales_delta_krw": market_delta_krw,
            "market_sales_delta_억원": round(market_delta_krw / 100_000_000, 2),
            "market_delta_pct": market_delta_pct,
            "delta_pct_gap": gap,
            "comparison_relation": relation,
            "calculation": "brand and market sales pct change over requested month",
        },
    }


def _brand_trend_comparison_call(calls: list[dict[str, Any]], anchor_brand: str) -> dict[str, Any] | None:
    series_by_brand = _series_points_by_brand_and_metric(calls)
    anchor = series_by_brand.get(anchor_brand)
    if not anchor:
        return None
    comparison_brand = next((brand for brand, points in series_by_brand.items() if brand != anchor_brand and len(points) >= 2), "")
    if not comparison_brand:
        return None
    comparison = series_by_brand[comparison_brand]
    common_periods = [period for period in sorted(anchor) if period in comparison]
    if len(common_periods) < 2:
        return None
    start_period, end_period = common_periods[0], common_periods[-1]
    anchor_start = anchor[start_period]
    anchor_end = anchor[end_period]
    comparison_start = comparison[start_period]
    comparison_end = comparison[end_period]
    anchor_share_delta = round(float(anchor_end.get("share") or 0) - float(anchor_start.get("share") or 0), 4)
    comparison_share_delta = round(float(comparison_end.get("share") or 0) - float(comparison_start.get("share") or 0), 4)
    anchor_sales_delta_pct = _pct_change(float(anchor_start.get("sales") or 0), float(anchor_end.get("sales") or 0))
    comparison_sales_delta_pct = _pct_change(float(comparison_start.get("sales") or 0), float(comparison_end.get("sales") or 0))
    signal = _threat_signal(anchor_share_delta, comparison_share_delta, anchor_sales_delta_pct, comparison_sales_delta_pct)
    period = f"{start_period}→{end_period}"
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": (
            f"{period} {comparison_brand} MS 변화는 {comparison_share_delta:.2f}%p, "
            f"{anchor_brand} MS 변화는 {anchor_share_delta:.2f}%p입니다."
        ),
        "render_data": {
            "brand": anchor_brand,
            "comparison_brand": comparison_brand,
            "metric": "brand_trend_comparison",
            "period": period,
            "from_period": start_period,
            "to_period": end_period,
            "brand_from_ms_pct": anchor_start.get("share"),
            "brand_to_ms_pct": anchor_end.get("share"),
            "brand_share_delta_pctp": anchor_share_delta,
            "brand_from_sales_krw": anchor_start.get("sales"),
            "brand_to_sales_krw": anchor_end.get("sales"),
            "brand_sales_delta_pct": anchor_sales_delta_pct,
            "comparison_from_ms_pct": comparison_start.get("share"),
            "comparison_to_ms_pct": comparison_end.get("share"),
            "comparison_share_delta_pctp": comparison_share_delta,
            "comparison_from_sales_krw": comparison_start.get("sales"),
            "comparison_to_sales_krw": comparison_end.get("sales"),
            "comparison_sales_delta_pct": comparison_sales_delta_pct,
            "comparison_signal": signal,
            "calculation": "two-brand sales/share trend comparison for evidence-based causal analysis",
        },
    }


def _competitive_insight_signals_call(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    data = next(
        (
            _metric_data(call)
            for call in calls
            if isinstance(_metric_data(call).get("level_top5_trend_series"), list)
        ),
        None,
    )
    if data is None:
        return None
    trends = [item for item in data.get("level_top5_trend_series", []) if isinstance(item, dict)]
    if len(trends) < 2:
        return None
    market_start, market_end = _series_edge_values(data.get("market_size_series"))
    market_delta = market_end - market_start if market_start is not None and market_end is not None else None
    market_growth = _pct_change(market_start, market_end) if market_start not in (None, 0) and market_end is not None else None
    raw_signals = [_competitive_signal_for_trend(item, market_delta, market_growth) for item in trends]
    signals = [signal for signal in raw_signals if signal]
    if not signals:
        return None
    _add_cohort_relative_signals(signals)
    signals = sorted(signals, key=lambda item: abs(float(item.get("share_delta_pctp") or 0)), reverse=True)
    top_gainer = max(signals, key=lambda item: float(item.get("share_delta_pctp") or 0))
    top_faller = min(signals, key=lambda item: float(item.get("share_delta_pctp") or 0))
    gain_loss_ratio = _gain_loss_ratio(top_gainer, top_faller)
    return {
        "source": "UBIST",
        "tool": "agent_calculation",
        "summary_text": "상위 브랜드 시계열에서 share-of-growth, 성장분해, gain-loss, cohort 상대화 신호를 계산했습니다.",
        "render_data": {
            "metric": "competitive_insight_signals",
            "period": _trend_period_range(trends),
            "market_delta_krw": market_delta,
            "market_delta_억원": round(market_delta / 100_000_000, 2) if isinstance(market_delta, int | float) else None,
            "market_growth_pct": market_growth,
            "signals": signals,
            "top_gainer": top_gainer if float(top_gainer.get("share_delta_pctp") or 0) > 0 else None,
            "top_faller": top_faller if float(top_faller.get("share_delta_pctp") or 0) < 0 else None,
            "gain_loss_ratio_pct": gain_loss_ratio,
            "surface_policy": {"gain_loss_ratio_pct": "internal_only"},
            "calculation": "deterministic top-brand trend insight for evidence-based causal analysis",
        },
    }


def _competitive_signal_for_trend(
    item: dict[str, Any],
    market_delta: float | None,
    market_growth: float | None,
) -> dict[str, Any]:
    brand = str(item.get("brand") or "")
    if not brand:
        return {}
    start_value, end_value = _series_edge_values(item.get("series"))
    if start_value is None or end_value is None:
        return {}
    value_delta = _number(item.get("value_delta_krw"))
    if value_delta is None:
        value_delta = end_value - start_value
    share_delta = _number(item.get("share_delta_pctp"))
    if share_delta is None:
        share_delta = _share_delta_from_series(item.get("series"))
    brand_growth = _pct_change(start_value, end_value)
    share_of_growth = round(value_delta / market_delta * 100, 2) if market_delta not in (None, 0) else None
    period_from, period_to = _series_edge_periods(item.get("series"))
    return {
        "brand": brand,
        "rank": item.get("rank"),
        "latest_share_pct": item.get("ms_recent_pct"),
        "share_delta_pctp": share_delta,
        "period_from": period_from,
        "period_to": period_to,
        "comparison_basis": "analysis_period" if period_from and period_to else None,
        "value_delta_krw": value_delta,
        "value_delta_억원": round(value_delta / 100_000_000, 2) if isinstance(value_delta, int | float) else None,
        "brand_growth_pct": brand_growth,
        "market_growth_pct": market_growth,
        "excess_growth_vs_market_pct": round(brand_growth - market_growth, 4) if market_growth is not None else None,
        "share_of_growth_pct": share_of_growth,
    }


def _add_cohort_relative_signals(signals: list[dict[str, Any]]) -> None:
    deltas = [float(item["share_delta_pctp"]) for item in signals if isinstance(item.get("share_delta_pctp"), int | float)]
    if len(deltas) < 2:
        return
    mean = sum(deltas) / len(deltas)
    variance = sum((value - mean) ** 2 for value in deltas) / len(deltas)
    std = math.sqrt(variance)
    ordered = sorted(deltas)
    for item in signals:
        delta = item.get("share_delta_pctp")
        if not isinstance(delta, int | float):
            continue
        item["z_score"] = round((float(delta) - mean) / std, 2) if std else 0.0
        item["percentile"] = round((sum(1 for value in ordered if value <= float(delta)) / len(ordered)) * 100, 2)


def _gain_loss_ratio(gainer: dict[str, Any], faller: dict[str, Any]) -> float | None:
    gain = float(gainer.get("share_delta_pctp") or 0)
    loss = abs(float(faller.get("share_delta_pctp") or 0))
    if gain <= 0 or loss <= 0:
        return None
    return round(gain / loss * 100, 2)


def _series_edge_periods(raw_series: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw_series, list) or not raw_series:
        return None, None
    first = raw_series[0]
    last = raw_series[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        return None, None
    start = first.get("period")
    end = last.get("period")
    return (str(start) if start else None, str(end) if end else None)


def _series_edge_values(raw_series: Any) -> tuple[float | None, float | None]:
    if not isinstance(raw_series, list) or len(raw_series) < 2:
        return (None, None)
    values = [
        float(item["value_krw"])
        for item in raw_series
        if isinstance(item, dict) and isinstance(item.get("value_krw"), int | float)
    ]
    if len(values) < 2:
        return (None, None)
    return (values[0], values[-1])


def _share_delta_from_series(raw_series: Any) -> float | None:
    if not isinstance(raw_series, list) or len(raw_series) < 2:
        return None
    values = [
        float(item["ms_pct"])
        for item in raw_series
        if isinstance(item, dict) and isinstance(item.get("ms_pct"), int | float)
    ]
    if len(values) < 2:
        return None
    return round(values[-1] - values[0], 4)


def _trend_period_range(trends: list[dict[str, Any]]) -> str:
    for item in trends:
        series = item.get("series")
        if not isinstance(series, list) or len(series) < 2:
            continue
        start = next((point.get("period") for point in series if isinstance(point, dict) and point.get("period")), "")
        end = next((point.get("period") for point in reversed(series) if isinstance(point, dict) and point.get("period")), "")
        if start and end:
            return f"{start}→{end}"
    return ""


def _value_points(raw_series: Any) -> dict[str, float]:
    points: dict[str, float] = {}
    if not isinstance(raw_series, list):
        return points
    for item in raw_series:
        if not isinstance(item, dict):
            continue
        period = item.get("period")
        value = item.get("value_krw")
        if isinstance(period, str) and isinstance(value, int | float):
            points[period] = float(value)
    return points


def _series_points_by_brand_and_metric(calls: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]]:
    grouped: dict[str, dict[str, dict[str, float]]] = {}
    for call in calls:
        data = _metric_data(call)
        brand = str(data.get("brand") or "")
        series = data.get("brand_value_series_10pt")
        if not brand or not isinstance(series, list):
            continue
        for item in series:
            if not isinstance(item, dict):
                continue
            period = item.get("period")
            sales = item.get("value_krw")
            share = item.get("ms_pct")
            if isinstance(period, str) and isinstance(sales, int | float):
                grouped.setdefault(brand, {})[period] = {"sales": float(sales), "share": float(share) if isinstance(share, int | float) else 0.0}
    return grouped


def _target_month_period(question: str, points: dict[str, float], *, month: str) -> str:
    explicit = f"{int(month)}월"
    if explicit not in question and f"{month}월" not in question:
        return ""
    candidates = [period for period in points if _is_month_period(period) and period.endswith(f"-{month}")]
    return sorted(candidates)[-1] if candidates else ""


def _previous_month(period: str) -> str:
    year = int(period[:4])
    month = int(period[5:])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _pct_change(start_value: float, end_value: float) -> float:
    return round((end_value / start_value - 1) * 100, 4) if start_value else 0.0


def _market_vs_brand_relation(brand_pct: float, market_pct: float, gap: float) -> str:
    if brand_pct < 0 and market_pct < 0:
        if gap < -3:
            return "brand_declined_more_than_market"
        if gap > 3:
            return "brand_declined_less_than_market"
        return "same_direction_market_down"
    if brand_pct < 0 <= market_pct:
        return "brand_specific_weakness_signal"
    if brand_pct >= 0 > market_pct:
        return "brand_outperformed_falling_market"
    return "same_direction_growth"


def _threat_signal(anchor_share_delta: float, comparison_share_delta: float, anchor_sales_delta_pct: float, comparison_sales_delta_pct: float) -> str:
    if comparison_share_delta > anchor_share_delta and comparison_sales_delta_pct >= anchor_sales_delta_pct:
        return "comparison_outpaced_anchor_trend"
    if comparison_share_delta > 0 and anchor_share_delta <= 0:
        return "comparison_gaining_while_anchor_flat_or_down"
    return "no_clear_outpacing_signal"


def _sales_series_points(calls: list[dict[str, Any]]) -> list[tuple[str, float]]:
    grouped = _sales_series_points_by_brand(calls)
    if not grouped:
        return []
    return next(iter(grouped.values()))


def _series_brand(calls: list[dict[str, Any]]) -> str:
    for call in calls:
        data = _metric_data(call)
        if data.get("brand_value_series_10pt"):
            return str(data.get("brand") or "")
    return ""


def _metric_data(call: dict[str, Any]) -> dict[str, Any]:
    data = call.get("render_data")
    return data if isinstance(data, dict) else {}


def _answer_brand(question: str, resolver: BrandResolver) -> str:
    try:
        return resolver.resolve(question, allow_default=False).canonical_brand
    except UnsupportedBrandError:
        return "agent loop"


def _pre_resolved_brands(question: str, resolver: BrandResolver) -> tuple[str, ...]:
    return tuple(item.canonical_brand for item in _pre_resolutions(question, resolver))


def _pre_resolutions(question: str, resolver: BrandResolver):
    try:
        return resolver.resolve_many(question, allow_default=False)
    except UnsupportedBrandError:
        return ()


def _comparison_brand_grounding(
    question: str,
    resolver: BrandResolver,
    base_allowed_brands: tuple[str, ...],
) -> ComparisonBrandGrounding:
    supported: list[str] = []
    unsupported: list[str] = []
    for term in _comparison_brand_terms(question):
        try:
            canonical = resolver.resolve(term, allow_default=False).canonical_brand
        except UnsupportedBrandError:
            if term not in base_allowed_brands:
                unsupported.append(term)
            continue
        if canonical not in base_allowed_brands:
            supported.append(canonical)
    return ComparisonBrandGrounding(tuple(dict.fromkeys(supported)), tuple(dict.fromkeys(unsupported)))


def _comparison_brand_terms(question: str) -> tuple[str, ...]:
    terms = []
    for term in ("아토젯",):
        if term in question:
            terms.append(term)
    return tuple(terms)


def _unsupported_comparison_metric_call(term: str) -> dict[str, Any]:
    message = f"{term} 매출 변화는 현재 지원 브랜드 목록에서 지표 조회 대상을 확정하지 못했습니다."
    return {
        "source": "cache",
        "tool": "unsupported_metric",
        "summary_text": message,
        "render_data": {"brand": term, "metric": "sales_delta", "status": "unsupported", "message": message},
    }


def _market_member_segment_metric_call(anchor_brand: str, term: str, metrics: MetricsTool, query_layer: StrategicQueryLayer | None = None) -> dict[str, Any] | None:
    if query_layer is not None:
        try:
            return query_layer.market_member_metric(anchor_brand, term)
        except (LookupError, TypeError, ValueError):
            pass
    trend_call = _market_member_trend_metric_call(anchor_brand, term, metrics)
    if trend_call is not None:
        return trend_call
    try:
        level_call = metrics.get_brand_metric(anchor_brand, metric="market_share", filter_entries=(("level", "Brand"),))
    except (LookupError, TypeError, ValueError, KeyError):
        return None
    data = _metric_data(level_call)
    segment = _segment_for_brand(data.get("level_segments"), term)
    if segment is None:
        return None
    sales_krw = _number(segment.get("value"))
    ms_pct = _number(segment.get("ms_recent_pct"))
    rank = segment.get("rank")
    period = str(data.get("period") or "latest")
    source_label = data.get("source_label")
    sales_eok = round(sales_krw / 100_000_000, 2) if sales_krw is not None else None
    return {
        "source": "cache",
        "tool": "get_brand_metric",
        "summary_text": (
            f"{term}은 {anchor_brand} 시장 Brand segment에서 최신 MS "
            f"{ms_pct if ms_pct is not None else 'N/A'}%, 매출 {sales_eok if sales_eok is not None else 'N/A'}억원으로 확인됩니다."
        ),
        "render_data": {
            "brand": term,
            "metric": "market_member_snapshot",
            "period": period,
            "market_id": data.get("market_id"),
            "source_label": source_label,
            "sales_krw": sales_krw,
            "sales_억원": sales_eok,
            "ms_recent_pct": ms_pct,
            "rank": rank,
            "market_member_source_brand": anchor_brand,
            "data_scope": "market_member_level_segment",
        },
    }


def _market_member_trend_metric_call(anchor_brand: str, term: str, metrics: MetricsTool) -> dict[str, Any] | None:
    try:
        series_call = metrics.get_brand_metric(anchor_brand, metric="series")
    except (LookupError, TypeError, ValueError, KeyError):
        return None
    data = _metric_data(series_call)
    trend = _trend_for_brand(data.get("level_top5_trend_series"), term)
    if trend is None:
        return None
    series = trend.get("series")
    if not isinstance(series, list) or len(series) < 2:
        return None
    latest = series[-1] if isinstance(series[-1], dict) else {}
    sales_krw = _number(latest.get("value_krw"))
    sales_eok = round(sales_krw / 100_000_000, 2) if sales_krw is not None else None
    ms_pct = _number(latest.get("ms_pct"))
    rank = latest.get("rank") or trend.get("rank")
    period = str(latest.get("period") or data.get("period") or "latest")
    return {
        "source": "cache",
        "tool": "get_brand_metric",
        "summary_text": (
            f"{term}은 {anchor_brand} 시장 level_top5_trend에서 최신 MS "
            f"{ms_pct if ms_pct is not None else 'N/A'}%, 매출 {sales_eok if sales_eok is not None else 'N/A'}억원으로 확인됩니다."
        ),
        "render_data": {
            "brand": term,
            "metric": "market_member_series",
            "period": period,
            "market_id": data.get("market_id"),
            "source_label": data.get("source_label"),
            "sales_krw": sales_krw,
            "sales_억원": sales_eok,
            "ms_recent_pct": ms_pct,
            "rank": rank,
            "brand_value_series_10pt": series,
            "market_member_source_brand": anchor_brand,
            "data_scope": "market_member_level_top5_trend",
        },
    }


def _trend_for_brand(trends: Any, brand: str) -> dict[str, Any] | None:
    if not isinstance(trends, list):
        return None
    for item in trends:
        if not isinstance(item, dict):
            continue
        if item.get("brand") == brand:
            return item
    return None


def _segment_for_brand(segments: Any, brand: str) -> dict[str, Any] | None:
    if not isinstance(segments, list):
        return None
    for item in segments:
        if not isinstance(item, dict):
            continue
        if item.get("name") == brand or item.get("brand") == brand:
            return item
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _step_allowed_brands(base_allowed_brands: tuple[str, ...], observations: tuple[AgentObservation, ...]) -> tuple[str, ...]:
    brands = list(base_allowed_brands)
    for observation in observations:
        data = (observation.call or {}).get("render_data", {})
        if not isinstance(data, dict):
            continue
        for key in ("brand", "anchor_brand"):
            value = data.get(key)
            if value:
                brands.append(str(value))
        members = data.get("member_brands")
        if isinstance(members, tuple | list):
            brands.extend(str(member) for member in members)
    return tuple(dict.fromkeys(brands))


def _planner_allowed_brands(
    base_allowed_brands: tuple[str, ...],
    observations: tuple[AgentObservation, ...],
    *,
    expanded_members_exposed: bool,
) -> tuple[str, ...]:
    if not expanded_members_exposed:
        return _step_allowed_brands(base_allowed_brands, observations)
    brands = list(base_allowed_brands)
    for observation in observations:
        brands.extend(str(value) for key, value in observation.arguments.items() if key in {"brand", "comparison_brand"} and value)
        data = (observation.call or {}).get("render_data", {})
        if not isinstance(data, dict):
            continue
        brands.extend(str(data[key]) for key in ("brand", "anchor_brand") if data.get(key))
        segments = data.get("level_segments")
        if isinstance(segments, tuple | list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                value = segment.get("brand") or segment.get("name")
                if value:
                    brands.append(str(value))
    return tuple(dict.fromkeys(brands))


def _has_market_members(observations: tuple[AgentObservation, ...]) -> bool:
    for observation in observations:
        data = (observation.call or {}).get("render_data", {})
        if isinstance(data, dict) and isinstance(data.get("member_brands"), tuple | list):
            return True
    return False


_SUFFICIENT_METRIC_TOOLS = {
    "get_metric",
    "get_brand_sales",
    "get_brand_share",
    "get_brand_series",
    "compare_brands_series",
    "get_top_brands",
    "get_brand_channel_breakdown",
    "get_brand_specialty_breakdown",
    "query",
}
_FOLLOWUP_CONTEXT_TOKENS = (
    "뉴스",
    "이슈",
    "소식",
    "환자수",
    "환자 수",
    "질병",
    "질환",
    "HIRA",
    "임상",
    "clinical",
    "특허",
    "독점권",
    "라벨",
    "FDA",
    "허가",
    "식약처",
    "MFDS",
    "의약품정보",
    "디테일링",
    "연구",
    "결과",
    "같은 시장",
    "대비",
)


def _observation_is_sufficient_for_final_answer(
    question: str,
    observations: tuple[AgentObservation, ...],
    batch: tuple[AgentObservation, ...],
) -> bool:
    """Skip an extra LLM stop-decision when verified metric facts are enough."""
    if not observations or not batch:
        return False
    if any(token in question for token in _FOLLOWUP_CONTEXT_TOKENS):
        return False
    return any(_metric_observation_has_answer_fact(item) for item in batch)


def _explicit_period_metric_no_data(
    question: str,
    calls: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    if not canonical_periods(question):
        return None
    return next(
        (
            call
            for call in calls
            if str(call.get("tool") or "") == "get_brand_metric"
            and tool_call_status(call) == "no_data"
            and isinstance(call.get("render_data"), dict)
            and bool(call["render_data"].get("message"))
        ),
        None,
    )


def _metric_observation_has_answer_fact(item: AgentObservation) -> bool:
    if item.status != "ok":
        return False
    if item.tool_name not in _SUFFICIENT_METRIC_TOOLS:
        return False
    call = item.call if isinstance(item.call, dict) else {}
    if str(call.get("tool") or "") not in {"", "get_brand_metric"}:
        return False
    data = call.get("render_data")
    if not isinstance(data, dict):
        return bool(call.get("summary_text"))
    return any(
        key in data and data.get(key) not in (None, "", [], ())
        for key in (
            "sales_억원",
            "ms_recent_pct",
            "rank",
            "brand_value_series_10pt",
            "rows",
            "query_result_id",
            "market_size_억원",
            "sales_delta_억원",
            "share_delta_pct",
        )
    )


def _sources(calls: list[dict[str, Any]]) -> list[str]:
    sources: set[str] = set()
    for call in calls:
        source = str(call.get("source") or "")
        if not source:
            continue
        data = call.get("render_data")
        status = str(data.get("status") or "") if isinstance(data, dict) else ""
        tool = str(call.get("tool") or "")
        if tool in {"query_failed", "unsupported_metric"} or status in {
            "error",
            "query_failed",
            "unsupported",
            "mapping_failed",
            "missing",
            "incomplete_split",
        }:
            continue
        sources.add(source)
    return sorted(sources)


def _tool_selection(question: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    expected = _expected_tool_groups(question)
    selected = _selected_tool_groups(calls)
    if not expected:
        return {"expected_tool_groups": [], "selected_tool_groups": sorted(selected), "tool_selection_accuracy": None}
    hits = expected.intersection(selected)
    return {
        "expected_tool_groups": sorted(expected),
        "selected_tool_groups": sorted(selected),
        "tool_selection_accuracy": round(len(hits) / len(expected), 4),
    }


def _expected_tool_groups(question: str) -> set[str]:
    expected: set[str] = set()
    if any(token in question for token in ("뉴스", "이슈", "소식")):
        expected.add("news")
    if any(token in question for token in ("환자수", "환자 수", "질병", "질환", "HIRA")):
        expected.add("hira")
    if any(token in question for token in ("임상", "clinical")):
        expected.add("clinical")
    if any(token in question for token in ("특허", "patent", "Orange", "orange")):
        expected.add("patent")
    if any(token in question for token in ("매출", "점유율", "순위", "HHI", "시장")):
        expected.add("metric")
    if "같은 시장" in question:
        expected.add("market_scope")
    if "전" in question and "대비" in question:
        expected.add("relative_date")
    return expected


def _selected_tool_groups(calls: list[dict[str, Any]]) -> set[str]:
    selected: set[str] = set()
    for call in calls:
        tool = str(call.get("tool") or "")
        if tool == "get_brand_metric":
            selected.add("metric")
        if tool == "get_market_landscape":
            selected.add("market_scope")
        if tool == "resolve_relative_date":
            selected.add("relative_date")
        if tool == "deep_analysis_related_news":
            selected.add("news")
        if tool == "get_disease_stats":
            selected.add("hira")
        if tool == "search_clinical":
            selected.add("clinical")
        if tool == "search_patent":
            selected.add("patent")
    return selected


def _deep_batch_progress_detail(
    plans: tuple[ToolCallPlan, ...],
    additional_parallel_tools: Collection[str],
) -> str:
    parallel_tools = planned_parallel_tool_names(
        plans,
        additional_parallel_tools=additional_parallel_tools,
    )
    all_tools = {plan.name for plan in plans}
    parallel_groups = _deep_tool_groups(parallel_tools)
    serial_groups = _deep_tool_groups(all_tools.difference(parallel_tools))
    if parallel_groups and serial_groups:
        return f"{'·'.join(parallel_groups)} 동시 조회 · {'·'.join(serial_groups)} 순차 조회"
    if parallel_groups:
        return f"{'·'.join(parallel_groups)} 동시 조회"
    if serial_groups:
        return f"{'·'.join(serial_groups)} 순차 조회"
    return "관련 근거 조회"


def _deep_tool_groups(tool_names: set[str] | frozenset[str]) -> tuple[str, ...]:
    selected = {
        group
        for tool_name in tool_names
        if (group := _DEEP_TOOL_GROUP_BY_NAME.get(tool_name)) is not None
    }
    return tuple(group for group in _DEEP_TOOL_GROUP_ORDER if group in selected)
