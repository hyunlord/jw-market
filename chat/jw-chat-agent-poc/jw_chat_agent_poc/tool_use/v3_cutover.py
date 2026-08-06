from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import time
from typing import Protocol


LOGGER = logging.getLogger(__name__)
_TRUE = frozenset({"1", "true", "yes", "on"})
_GENERAL_HELP = (
    "시장, 브랜드, 기간, 지표를 포함해 질문하면 확인 가능한 근거를 조회해 답합니다. "
    "필수 정보가 모호하면 부족한 항목만 다시 확인합니다."
)


@dataclass(frozen=True, slots=True)
class V3CutoverConfig:
    enabled: bool = False
    question_types: frozenset[str] = frozenset({"uncovered"})
    domains: frozenset[str] = frozenset({"*"})
    overall_timeout_s: float = 60.0
    selection_timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> V3CutoverConfig:
        return cls(
            enabled=os.getenv("JW_CHAT_V3_CUTOVER_ENABLED", "").strip().casefold()
            in _TRUE,
            question_types=_csv_set(
                os.getenv("JW_CHAT_V3_CUTOVER_QUESTION_TYPES", "uncovered")
            ),
            domains=_csv_set(os.getenv("JW_CHAT_V3_CUTOVER_DOMAINS", "*")),
            overall_timeout_s=min(
                _positive_float(os.getenv("JW_CHAT_V3_CUTOVER_TIMEOUT_S"), 60.0),
                60.0,
            ),
            selection_timeout_s=_positive_float(
                os.getenv("JW_CHAT_V3_SELECTION_TIMEOUT_S"), 10.0
            ),
        )


@dataclass(frozen=True, slots=True)
class V3ServingResult:
    domain: str
    answer: str
    limitations: tuple[str, ...]
    sources: tuple[str, ...]
    charts: tuple[Mapping[str, object], ...]
    trace: Mapping[str, object]
    tool_calls: tuple[Mapping[str, object], ...]


class V3ServingPipeline(Protocol):
    def run(self, question: str) -> V3ServingResult: ...


def apply_v3_cutover(
    question: str,
    legacy_result: dict[str, object],
    *,
    config: V3CutoverConfig | None = None,
    pipeline_factory: Callable[[], V3ServingPipeline] | None = None,
) -> dict[str, object]:
    active = config or V3CutoverConfig.from_env()
    if not active.enabled or "uncovered" not in active.question_types:
        return legacy_result
    if not is_uncovered_legacy_result(legacy_result):
        return legacy_result

    factory = pipeline_factory or (lambda: _DefaultV3ServingPipeline(active))
    try:
        served = factory().run(question)
    except Exception:  # noqa: BLE001 - serving cutover must fail open to legacy
        LOGGER.exception("v3_cutover_failed_open")
        return legacy_result
    if not _domain_enabled(served.domain, active.domains):
        LOGGER.info("v3_cutover_domain_not_enabled domain=%s", served.domain)
        return legacy_result
    if not served.answer.strip() and not served.limitations:
        LOGGER.warning("v3_cutover_empty_validated_result")
        return legacy_result

    answer = _render_answer(served.answer, served.limitations)
    return {
        **legacy_result,
        "answer": answer,
        "sources": list(served.sources),
        "tool_calls": [dict(call) for call in served.tool_calls],
        "charts": [dict(chart) for chart in served.charts],
        "v3_cutover_ready": True,
        "v3_cutover_domain": served.domain,
        "v3_cutover_trace": dict(served.trace),
    }


def is_uncovered_legacy_result(result: Mapping[str, object]) -> bool:
    if str(result.get("answer") or "").strip() != _GENERAL_HELP:
        return False
    calls = result.get("tool_calls")
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)) and calls:
        return False
    routing = result.get("router_diagnostics")
    if not isinstance(routing, Mapping):
        return False
    v4 = routing.get("routing_v4")
    if not isinstance(v4, Mapping):
        return False
    proposal = v4.get("proposed_routing_signature")
    decision = proposal.get("routing_decision") if isinstance(proposal, Mapping) else None
    return isinstance(decision, Mapping) and decision.get("route_outcome") == "NO_TOOL"


def grounded_chart_specs(
    charts: Sequence[Mapping[str, object]],
    fact_raw_results: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    grounded: list[Mapping[str, object]] = []
    for chart in charts:
        refs = chart.get("evidence_refs")
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
            continue
        cited = [fact_raw_results[ref] for ref in refs if ref in fact_raw_results]
        if not cited:
            continue
        allowed = {literal for raw in cited for literal in _numeric_literals(raw)}
        values = _chart_numeric_literals(chart)
        allowed_labels = {label for raw in cited for label in _string_literals(raw)}
        labels = chart.get("labels")
        chart_labels = (
            {str(label) for label in labels}
            if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes))
            else set()
        )
        if values <= allowed and chart_labels <= allowed_labels:
            grounded.append(chart)
    return tuple(grounded)


def _dedupe_charts_by_data(
    charts: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    seen: set[str] = set()
    unique: list[Mapping[str, object]] = []
    for chart in charts:
        datasets = chart.get("datasets")
        series = (
            [
                dataset.get("data")
                for dataset in datasets
                if isinstance(dataset, Mapping)
            ]
            if isinstance(datasets, Sequence)
            and not isinstance(datasets, (str, bytes))
            else []
        )
        signature = json.dumps(
            {
                "type": chart.get("type"),
                "labels": chart.get("labels"),
                "series": series,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(chart)
    return tuple(unique)


class _DefaultV3ServingPipeline:
    def __init__(self, config: V3CutoverConfig) -> None:
        self._config = config

    def run(self, question: str) -> V3ServingResult:
        from jw_chat_agent_poc.agent_loop.factory import default_external_client
        from jw_chat_agent_poc.service.charts import (
            build_charts,
            issue_render_authorization,
        )
        from jw_chat_agent_poc.tool_use.v3_execution_factory import (
            build_default_shadow_executor,
        )
        from jw_chat_agent_poc.tool_use.v3_fusion import (
            FusionOutputTruncatedError,
            V3FusionEngine,
        )
        from jw_chat_agent_poc.tool_use.v3_fusion_provider import (
            GenosV3FusionProvider,
        )
        from jw_chat_agent_poc.tool_use.v3_selection import (
            V3ToolSelector,
            selection_tool_specs,
        )
        from jw_chat_agent_poc.tool_use.v3_selection_provider import (
            GenosV3ToolChoiceProvider,
        )
        from jw_chat_agent_poc.tool_use.v3_scope_view_set import (
            build_scope_view_set,
            merge_evidence_bundles,
            reconcile_view_limitations,
            scope_view_choices,
        )
        from jw_chat_agent_poc.tool_use.v3_web_augmentation import (
            V3WebAugmenter,
        )
        from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver

        started = time.monotonic()
        selection_provider = replace(
            GenosV3ToolChoiceProvider.from_env(),
            timeout_s=self._config.selection_timeout_s,
        )
        selector = V3ToolSelector(provider=selection_provider)
        selection = selector.select(question)
        if selection.unknown_tool_names:
            raise RuntimeError("V3 selector returned an unknown tool")
        domains_by_tool = {spec.name: spec.domain for spec in selection_tool_specs()}
        domains_by_tool["web_search"] = "web"
        selected_domains = {
            domains_by_tool.get(choice.name, "general") for choice in selection.choices
        }
        domain = _serving_domain(selected_domains)
        if not _domain_enabled(domain, self._config.domains):
            return V3ServingResult(domain, "", (), (), (), {"skipped": "domain"}, ())

        executor = build_default_shadow_executor(question)
        bundle = executor.execute(selection.choices)
        market_scope_resolver = MarketScopeResolver()
        scope_confirmed = (
            market_scope_resolver.has_explicit_anchor(question)
            or market_scope_resolver.has_explicit_named_market(question)
        )
        supplemental_choices = scope_view_choices(
            selection.choices,
            scope_confirmed=scope_confirmed,
        )
        supplemental = executor.execute(supplemental_choices)
        view_set = build_scope_view_set(
            merge_evidence_bundles(bundle, supplemental),
            scope_confirmed=scope_confirmed,
        )
        external = default_external_client("live")
        augmented = V3WebAugmenter(
            search=lambda query, **kwargs: _web_search(
                external,
                query,
                topic=str(kwargs.get("topic") or "general"),
            )
        ).augment(question, bundle)
        combined = merge_evidence_bundles(augmented.bundle, supplemental)
        elapsed = time.monotonic() - started
        remaining = self._config.overall_timeout_s - elapsed - 0.5
        if remaining <= 1.0:
            return V3ServingResult(
                domain,
                "",
                ("응답 제한 시간 안에 근거 종합을 완료하지 못했습니다.",),
                (),
                (),
                {"reason_code": "v3_cutover_deadline_exhausted"},
                _legacy_tool_calls(combined.executions),
            )
        provider = GenosV3FusionProvider.from_env()
        provider = replace(provider, timeout_s=min(provider.timeout_s, remaining))
        try:
            fusion = V3FusionEngine(provider).generate(question, augmented.bundle)
        except FusionOutputTruncatedError as exc:
            return V3ServingResult(
                domain,
                "",
                tuple(exc.limitations),
                _source_labels(combined.facts),
                (),
                {
                    "reason_code": exc.reason_code,
                    "partial_recovery_attempted": False,
                    "finish_reason": exc.provider.finish_reason,
                },
                _legacy_tool_calls(combined.executions),
            )

        answer_model = fusion.validated.answer
        answer = "\n\n".join(claim.text for claim in answer_model.claims)
        if view_set.attached:
            answer = "\n\n".join(part for part in (answer, view_set.markdown) if part)
        tool_calls = _legacy_tool_calls(combined.executions)
        provisional = {"tool_calls": list(tool_calls)}
        authorization = issue_render_authorization(
            provisional,
            question=question,
            answer=answer,
            enforce_binding=False,
        )
        candidates = build_charts(
            provisional,
            authorization=authorization,
            question=question,
            answer=answer,
        )
        fact_results = {
            fact.evidence_id: fact.raw_result for fact in combined.facts
        }
        candidates = [
            {
                **chart,
                "evidence_refs": _chart_evidence_refs(chart, combined.facts),
            }
            for chart in candidates
        ]
        charts = _dedupe_charts_by_data(
            grounded_chart_specs((*candidates, *view_set.charts), fact_results)
        )
        model_limitations = reconcile_view_limitations(
            answer_model.limitations,
            view_set.view_names,
        )
        return V3ServingResult(
            domain=domain,
            answer=answer,
            limitations=(*model_limitations, *view_set.limitations),
            sources=_source_labels(combined.facts),
            charts=charts,
            trace={
                "selected_tools": [choice.name for choice in selection.choices],
                "accepted_claim_count": len(answer_model.claims),
                "rejected_claim_count": len(fusion.validated.audit.rejected_claims),
                "evidence_fact_count": len(combined.facts),
                "scope_view_set_attached": view_set.attached,
                "scope_view_names": list(view_set.view_names),
                "scope_view_supplemental_tools": [
                    choice.name for choice in supplemental_choices
                ],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "finish_reason": fusion.provider.finish_reason,
            },
            tool_calls=tool_calls,
        )


def _web_search(client: object, query: str, *, topic: str) -> object:
    from jw_chat_agent_poc.tool_use.v3_web_augmentation import WebSearchResult

    call = client.web_search(query, max_results=5, topic=topic)
    items = call.render_data.get("items")
    safe_items = (
        tuple(item for item in items if isinstance(item, Mapping))
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes))
        else ()
    )
    return WebSearchResult(
        provider=call.source,
        query=query,
        items=safe_items,
        latency_ms=float(call.elapsed_ms or 0.0),
        status=call.status,
        error=None if call.status in {"ok", "fixture"} else call.summary_text,
    )


def _legacy_tool_calls(executions: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    calls: list[Mapping[str, object]] = []
    for execution in executions:
        raw = execution.raw_result
        render_data = raw.get("render_data") if isinstance(raw, Mapping) else None
        calls.append(
            {
                "tool": execution.tool_name,
                "arguments": dict(execution.arguments),
                "status": "ok",
                "render_data": dict(render_data) if isinstance(render_data, Mapping) else raw,
            }
        )
    return tuple(calls)


def _source_labels(facts: Sequence[object]) -> tuple[str, ...]:
    labels: list[str] = []
    for fact in facts:
        label = str(getattr(fact, "url", "") or getattr(fact, "tool_name", "")).strip()
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


def _chart_evidence_refs(
    chart: Mapping[str, object],
    facts: Sequence[object],
) -> list[str]:
    source = str(chart.get("source") or "")
    matching = [
        str(fact.evidence_id)
        for fact in facts
        if source.startswith(str(getattr(fact, "tool_name", "")))
    ]
    if matching:
        return matching
    if source.startswith("tool_calls."):
        return [str(fact.evidence_id) for fact in facts]
    return []


def _render_answer(answer: str, limitations: Sequence[str]) -> str:
    parts = [answer.strip()] if answer.strip() else []
    if limitations:
        parts.append("확인 제한:\n" + "\n".join(f"- {item}" for item in limitations))
    return "\n\n".join(parts)


def _domain_enabled(domain: str, enabled: frozenset[str]) -> bool:
    if "*" in enabled:
        return True
    requested = frozenset(item for item in domain.split("+") if item)
    return bool(requested) and requested <= enabled


def _serving_domain(domains: set[str]) -> str:
    if not domains:
        return "general"
    return "+".join(sorted(domains))


def _csv_set(raw: str) -> frozenset[str]:
    values = frozenset(item.strip().casefold() for item in raw.split(",") if item.strip())
    return values or frozenset({"*"})


def _positive_float(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default


def _numeric_literals(value: object) -> set[str]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, int | float | Decimal):
        return {_canonical_number(value)}
    if isinstance(value, Mapping):
        return {literal for item in value.values() for literal in _numeric_literals(item)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {literal for item in value for literal in _numeric_literals(item)}
    return set()


def _chart_numeric_literals(chart: Mapping[str, object]) -> set[str]:
    datasets = chart.get("datasets")
    if not isinstance(datasets, Sequence) or isinstance(datasets, (str, bytes)):
        return set()
    values: set[str] = set()
    for dataset in datasets:
        if isinstance(dataset, Mapping):
            values.update(_numeric_literals(dataset.get("data")))
    return values


def _string_literals(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {literal for item in value.values() for literal in _string_literals(item)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {literal for item in value for literal in _string_literals(item)}
    return set()


def _canonical_number(value: object) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    normalized = format(decimal.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


__all__ = [
    "V3CutoverConfig",
    "V3ServingResult",
    "apply_v3_cutover",
    "grounded_chart_specs",
    "is_uncovered_legacy_result",
]
