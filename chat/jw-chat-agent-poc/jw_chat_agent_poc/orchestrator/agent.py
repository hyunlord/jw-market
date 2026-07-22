from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
from typing import Any

from jw_chat_agent_poc.agent_loop import ToolUseAgent, should_use_agent_loop
from jw_chat_agent_poc.agent_loop.routing import is_top_n_intent
from jw_chat_agent_poc.agent_loop.factory import (
    ambiguous_brand_result,
    ChatAgentDependencyOverrides,
    build_chat_agent_dependencies,
    build_tool_use_agent,
    field_not_exposed_result,
    unsupported_brand_result,
    unsupported_hira_interface_result,
)
from jw_chat_agent_poc.agent_loop.bq_planner import preflight_bq_question
from jw_chat_agent_poc.agent_loop.structured_planner import preflight_structured_market_question
from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.agentic import FilterEntry, relevance_filter_entries, relevance_question_text, validate_metric_filters
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.orchestrator.external_notices import (
    external_unavailable_for_missing_molecule,
    seeded_false_positive_notice,
)
from jw_chat_agent_poc.orchestrator.hira_disease import (
    HIRA_DISEASE_MAPPINGS,
    hira_disease_anchor_brand,
    hira_disease_calls,
    is_hira_disease_question,
)
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.market_answer_contract import (
    market_ambiguity_message,
    market_membership_mismatch_message,
)
from jw_chat_agent_poc.orchestrator.narrative_intent import needs_market_series
from jw_chat_agent_poc.orchestrator.question_intent import (
    allows_background_news_context,
    metric_from_question,
    requires_brand,
)
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace, qa_trace_started_at
from jw_chat_agent_poc.common.timing import Timing, new_timing, stage
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate, requested_unavailable_source
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.resolver import AmbiguousBrandError, BrandResolution, BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.router import BQRouter, LLMFirstBQRouter
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall, resolve_patent_ingredient_query
from jw_chat_agent_poc.tools.external.policy import (
    annotate_clinical_call,
    clinical_scope_notice,
    combo_query,
    inapplicable_call,
    is_external_inapplicable_brand,
    label_patent_scope_notice,
    needs_seeded_false_positive_filter,
)
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer
from jw_chat_agent_poc.tool_use.contracts import FallbackCode
from jw_chat_agent_poc.tool_use.integration import (
    attach_routing_v4_legacy_observation,
    external_tool_agent_enabled,
    run_external_tool_agent,
)
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import default_capability_matrix
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tool_use.routing_v4_runtime import configured_routing_mode
from jw_chat_agent_poc.tool_use.routing_v4_types import RoutingMode


class ChatAgent:
    def __init__(
        self,
        external_mode: str = "fixture",
        router: BQRouter | LLMFirstBQRouter | None = None,
        resolver: BrandResolver | None = None,
        metrics: MetricsTool | None = None,
        external: ExternalApiClient | None = None,
        news: DeepAnalysisNewsTool | None = None,
        rag: LocalDocumentRag | None = None,
        agent_loop: ToolUseAgent | None = None,
        query_layer: StrategicQueryLayer | None = None,
    ) -> None:
        dependencies = build_chat_agent_dependencies(
            external_mode=external_mode,
            overrides=ChatAgentDependencyOverrides(
                router=router,
                resolver=resolver,
                metrics=metrics,
                external=external,
                news=news,
                rag=rag,
                query_layer=query_layer,
            ),
        )
        self.router = dependencies.router
        self.resolver = dependencies.resolver
        self.metrics = dependencies.metrics
        self.external = dependencies.external
        self.news = dependencies.news
        self.rag = dependencies.rag
        self.agent_loop = agent_loop
        self.query_layer = dependencies.query_layer
        self._agent_loop_dependencies = dependencies.agent_loop_dependencies()

    def answer(self, question: str, documents: list[Path] | None = None) -> dict[str, Any]:
        timing = new_timing()
        docs = documents or []
        conversation_fallback = _conversation_fallback(question) if not docs else None
        if conversation_fallback is not None:
            conversation_fallback["timing"] = timing
            return conversation_fallback
        external_fallback_code: str | None = None
        source_trap = requested_unavailable_source(question)
        agent_source_trap = requested_unavailable_source(question, identity_only=True)
        pre_resolved: BrandResolution | None = None

        def finish(payload: dict[str, Any]) -> dict[str, Any]:
            annotated = _annotate_external_tool_fallback(payload, external_fallback_code)
            return attach_routing_v4_legacy_observation(
                question,
                annotated,
                resolver=self.resolver,
                external=self.external,
            )

        if not docs:
            classification = classify_question(question)
            capability = default_capability_matrix().resolve(
                classification.source_domain,
                classification.requested_capability,
            )
            if capability.status.value == "FIELD_NOT_EXPOSED":
                routes = BQRouter().route(question, has_documents=False)
                return finish(
                    field_not_exposed_result(
                        question,
                        capability.requested_capability,
                        routes,
                        router_diagnostics(self.router),
                    )
                )
            if is_hira_disease_question(question):
                try:
                    pre_resolved = self.resolver.resolve(question, allow_default=False)
                except UnsupportedBrandError:
                    disease_anchor = hira_disease_anchor_brand(question)
                    if disease_anchor is None:
                        routes = BQRouter().route(question, has_documents=False)
                        return finish(
                            unsupported_hira_interface_result(
                                question,
                                routes,
                                router_diagnostics(self.router),
                            )
                        )
                    pre_resolved = self.resolver.resolve(disease_anchor, allow_default=False)

        if external_tool_agent_enabled() and agent_source_trap is None:
            tool_pack_routes = BQRouter().route(question, has_documents=bool(docs))
            if _is_external_tool_agent_candidate(tool_pack_routes, docs, question=question):
                tool_result, pre_resolved, external_fallback_code = self._attempt_external_tool_agent(
                    question,
                    pre_resolved,
                    timing=timing,
                )
                if tool_result is not None:
                    return finish(tool_result)

        if (
            not docs
            and source_trap is None
            and self.agent_loop is None
            and self.query_layer is not None
            and (
                preflight_bq_question(question, self.resolver) is not None
                or preflight_structured_market_question(question, self.resolver) is not None
            )
        ):
            loop = build_tool_use_agent(self._agent_loop_dependencies)
            result = loop.answer(question)
            diagnostics = result.setdefault("router_diagnostics", {})
            if isinstance(diagnostics, dict):
                diagnostics["question_decomposition_bypassed"] = True
            return finish(result)

        with stage(timing, "question_decomposition", "BQ and tool routing"):
            routes = self.router.route(question, has_documents=bool(docs))
        if (
            external_tool_agent_enabled()
            and external_fallback_code is None
            and _is_external_tool_agent_candidate(routes, docs, question=question)
            and agent_source_trap is None
        ):
            tool_result, pre_resolved, external_fallback_code = self._attempt_external_tool_agent(
                question,
                pre_resolved,
                timing=timing,
            )
            if tool_result is not None:
                return finish(tool_result)
        if not docs and _is_known_ingredient_patent_question(question):
            loop = self.agent_loop or build_tool_use_agent(self._agent_loop_dependencies)
            return finish(loop.answer(question))
        requires_brand_flag = requires_brand(routes) and not is_hira_disease_question(question)
        portfolio_scope = not docs and is_portfolio_decline_question(question, routes) and should_use_agent_loop(question)
        if portfolio_scope:
            loop = self.agent_loop or build_tool_use_agent(self._agent_loop_dependencies)
            return finish(loop.answer(question))
        try:
            with stage(timing, "agent_pre_resolve", "brand resolver"):
                resolution = (
                    pre_resolved
                    if pre_resolved is not None
                    else self.resolver.resolve(question, allow_default=False)
                )
        except AmbiguousBrandError as exc:
            return finish(
                ambiguous_brand_result(
                    question,
                    routes,
                    router_diagnostics(self.router),
                    exc.candidates,
                )
            )
        except UnsupportedBrandError:
            disease_anchor = hira_disease_anchor_brand(question)
            if disease_anchor is not None:
                resolution = self.resolver.resolve(disease_anchor, allow_default=False)
            elif docs:
                resolution = _document_resolution()
            else:
                return finish(self._unsupported_brand(question, routes))
        if resolution.has_market_membership_mismatch and not docs:
            return finish(_market_membership_mismatch_result(question, resolution))
        if resolution.requires_market_clarification and not docs:
            return finish(_market_ambiguity_result(question, resolution))
        if resolution.support_source == "document_context" and any("metrics" in route.sources for route in routes):
            return finish(_brand_clarification_result(question))
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        sources: list[str] = []
        if source_trap is not None and not docs:
            return finish(self._requested_source_unavailable(question, resolution, routes, source_trap))

        if not docs and should_use_agent_loop(question, has_brand_anchor=True):
            loop = self.agent_loop or build_tool_use_agent(self._agent_loop_dependencies)
            return finish(loop.answer(question))

        if any("none" in route.sources for route in routes):
            return finish(self._no_data(question, resolution, routes))

        if any("deep_analysis_events" in route.sources for route in routes):
            news_filters = tuple(entry for route in routes if "deep_analysis_events" in route.sources for entry in route.filters)
            news_brands = self._news_brands(question, routes, resolution.canonical_brand)
            news_filters = (*news_filters, *relevance_filter_entries(news_brands, question))
            news_started_at = qa_trace_started_at()
            call = self.news.related_news(news_brands[0], filter_entries=news_filters)
            attach_tool_qa_trace(call, started_at=news_started_at)
            calls.append(call)
            sources.append(call["source"])

        if any("metrics" in route.sources for route in routes):
            market = resolution.market_id
            metric = metric_from_question(question)
            metric_filters = tuple(entry for route in routes if "metrics" in route.sources for entry in route.filters)
            filter_plan = validate_metric_filters(metric_filters)
            effective_filters = metric_filters if filter_plan.has_effective_filter else ()
            metric_started_at = qa_trace_started_at()
            with stage(timing, "tool:get_brand_metric", f"metric={metric}"):
                brand_metric_call = self._metric_call(
                    resolution.canonical_brand,
                    metric=metric,
                    filter_entries=effective_filters,
                    market=market,
                    prefer_mart=_prefer_mart_metric(resolution.support_source),
                )
            attach_tool_qa_trace(brand_metric_call, started_at=metric_started_at)
            metric_calls = [brand_metric_call]
            scope = _answer_scope(question)
            if scope is not None:
                for metric_call in metric_calls:
                    data = metric_call.get("render_data")
                    if not isinstance(data, dict):
                        continue
                    if scope == "single_brand_trend" and data.get("metric") not in {"series", "trend"}:
                        continue
                    data["answer_scope"] = scope
            if (
                self.query_layer is None
                and market is not None
                and not effective_filters
                and metric not in {"hhi", "series", "trend", "momentum", "ei"}
            ):
                landscape_started_at = qa_trace_started_at()
                with stage(timing, "tool:get_market_landscape", f"market={market}"):
                    market_landscape_call = self.metrics.get_market_landscape(market)
                attach_tool_qa_trace(market_landscape_call, started_at=landscape_started_at)
                metric_calls.insert(0, market_landscape_call)
            for call in metric_calls:
                calls.append(call)
                sources.append(call["source"])

        if self._should_attach_background_news(question, calls):
            background_started_at = qa_trace_started_at()
            call = self.news.related_news(resolution.canonical_brand, limit=3)
            data = call.setdefault("render_data", {})
            data["facade_tool"] = "background_news_context"
            data["context_role"] = "background_insight"
            data["provenance"] = {"source": "events/event_brand_scores", "mode": "full_corpus_or_cache_fallback"}
            attach_tool_qa_trace(call, started_at=background_started_at)
            calls.append(call)
            sources.append(call["source"])

        if any("external_api" in route.sources for route in routes):
            external_calls = self._external_calls(question, resolution, timing=timing)
            for call in external_calls:
                calls.append(call.__dict__)
                if call.tool == "matching_policy_notice":
                    notices.append(call.summary_text)
                sources.append(call.source)

        if docs and any("document" in route.sources for route in routes):
            document_started_at = qa_trace_started_at()
            rag_result = self.rag.search(question, docs)
            document_call = {"tool": "document_rag", **rag_result.__dict__}
            attach_tool_qa_trace(document_call, started_at=document_started_at, status="ok")
            calls.append(document_call)
            sources.append(rag_result.source)

        with stage(timing, "fact_assembly", "markdown fact set build"):
            markdown = MarkdownResponseBuilder().build(
                brand=resolution.canonical_brand,
                calls=calls,
                sources=sources,
                notices=notices,
            )
            answer = apply_requested_source_trap_gate(question, markdown.markdown)
        return finish({
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "router_diagnostics": router_diagnostics(self.router),
            "tool_calls": calls,
            "answer": answer,
            "markdown_response": markdown.to_dict(),
            "sources": sorted(set(sources)),
            "timing": timing,
        })

    def _news_brands(self, question: str, routes: list[Any], fallback_brand: str) -> tuple[str, ...]:
        source_text = relevance_question_text(question)
        try:
            resolutions = self.resolver.resolve_many(source_text, allow_default=False)
        except UnsupportedBrandError:
            return (fallback_brand,)
        resolved_brands = tuple(item.canonical_brand for item in resolutions)
        resolved_set = set(resolved_brands)
        routed_brands = tuple(
            brand
            for route in routes
            if "deep_analysis_events" in route.sources
            for brand in route.brands
            if brand in resolved_set
        )
        if routed_brands and set(routed_brands) == set(resolved_brands):
            return tuple(dict.fromkeys(routed_brands))
        return resolved_brands or (fallback_brand,)

    @staticmethod
    def _should_attach_background_news(question: str, calls: list[dict[str, Any]]) -> bool:
        if any(call.get("tool") == "deep_analysis_related_news" for call in calls):
            return False
        if any(token in question for token in ("뉴스", "이슈", "소식", "기사")):
            return False
        if not allows_background_news_context(question):
            return False
        for call in calls:
            if call.get("tool") not in {"get_brand_metric", "get_market_landscape"}:
                continue
            data = call.get("render_data")
            if isinstance(data, dict) and data.get("status") != "unsupported":
                return True
        return False

    def _external_calls(self, question: str, resolution, *, timing: Timing | None = None) -> list[ExternalCall]:
        lower = question.lower()
        calls: list[ExternalCall] = []
        if is_hira_disease_question(question):
            with stage(timing, "tool:hira_disease", resolution.canonical_brand):
                return hira_disease_calls(question, resolution, self.external)
        needs_molecule = (
            "임상" in question
            or "clinical" in lower
            or "fda" in lower
            or "라벨" in question
            or "label" in lower
            or "특허" in question
            or "patent" in lower
            or "orange" in lower
        )
        if needs_molecule and not resolution.molecule_en:
            return [external_unavailable_for_missing_molecule(resolution)]
        if needs_molecule and is_external_inapplicable_brand(resolution.canonical_brand):
            return [inapplicable_call(resolution.canonical_brand, resolution.molecule_en)]
        if "임상" in question or "clinical" in lower:
            if resolution.is_combo:
                with stage(timing, "tool:clinicaltrials_v2_search", "combo_and"):
                    calls.append(
                        annotate_clinical_call(
                            self.external.clinicaltrials_v2_search(combo_query(resolution.molecule_en)),
                            resolution.canonical_brand,
                            resolution.molecule_en,
                            "combo_and",
                        )
                    )
                for molecule in resolution.molecule_en:
                    with stage(timing, "tool:clinicaltrials_v2_search", molecule):
                        calls.append(
                            annotate_clinical_call(
                                self.external.clinicaltrials_v2_search(molecule),
                                resolution.canonical_brand,
                                (molecule,),
                                "component_reference",
                            )
                        )
            else:
                with stage(timing, "tool:clinicaltrials_v2_search", "molecule_trend"):
                    calls.append(
                        annotate_clinical_call(
                            self.external.clinicaltrials_v2_search(" OR ".join(resolution.molecule_en)),
                            resolution.canonical_brand,
                            resolution.molecule_en,
                            "molecule_trend",
                        )
                    )
            with stage(timing, "tool:mfds_clinical_trial_kr", resolution.canonical_brand):
                calls.append(self.external.mfds_clinical_trial_kr(resolution.canonical_brand))
            with stage(timing, "tool:clinical_scope_notice", resolution.canonical_brand):
                calls.append(clinical_scope_notice(resolution.canonical_brand, resolution.molecule_en, resolution.is_combo).to_call())
            if needs_seeded_false_positive_filter(resolution.canonical_brand):
                calls.append(seeded_false_positive_notice(resolution))
        if "fda" in lower or "라벨" in question or "label" in lower:
            if resolution.is_combo:
                with stage(timing, "tool:openfda_combo_label_search", resolution.canonical_brand):
                    calls.append(self.external.openfda_combo_label_search(resolution.molecule_en))
            for molecule in resolution.molecule_en:
                with stage(timing, "tool:openfda_label_search", molecule):
                    calls.append(self.external.openfda_label_search(molecule))
        if "특허" in question or "patent" in lower or "orange" in lower:
            for molecule in resolution.molecule_en:
                with stage(timing, "tool:mfds_patent", molecule):
                    calls.append(self.external.mfds_patent(molecule))
                with stage(timing, "tool:mfds_fda_orangebook", molecule):
                    calls.append(self.external.mfds_fda_orangebook(molecule))
            with stage(timing, "tool:matching_policy_notice", resolution.canonical_brand):
                calls.append(label_patent_scope_notice(resolution.canonical_brand, resolution.molecule_en).to_call())
            competitor_context = self._competitor_patent_context_call(question, resolution, timing=timing)
            if competitor_context is not None:
                calls.append(competitor_context)
        if not calls:
            with stage(timing, "tool:mfds_permission_search", resolution.canonical_brand):
                calls.append(self.external.mfds_permission_search(resolution.canonical_brand))
        return calls

    def _attempt_external_tool_agent(
        self,
        question: str,
        pre_resolved: BrandResolution | None,
        *,
        timing: Timing | None = None,
    ) -> tuple[dict[str, Any] | None, BrandResolution | None, str | None]:
        fixture_alias_check = getattr(self.resolver, "has_fixture_alias", None)
        should_pre_resolve = not callable(fixture_alias_check) or fixture_alias_check(question)
        if pre_resolved is None and should_pre_resolve:
            try:
                pre_resolved = self.resolver.resolve(question, allow_default=False)
            except UnsupportedBrandError:
                pre_resolved = None
        if pre_resolved is not None and pre_resolved.has_market_membership_mismatch:
            return _market_membership_mismatch_result(question, pre_resolved), pre_resolved, None
        if pre_resolved is not None and pre_resolved.requires_market_clarification:
            return _market_ambiguity_result(question, pre_resolved), pre_resolved, None
        tool_result = run_external_tool_agent(
            question,
            resolver=self.resolver,
            external=self.external,
            timing=timing,
        )
        diagnostics = tool_result.get("router_diagnostics")
        fallback_code = diagnostics.get("fallback_code") if isinstance(diagnostics, dict) else None
        if fallback_code in {
            None,
            FallbackCode.UNSUPPORTED_QUERY.value,
            FallbackCode.VERIFICATION_FAIL.value,
        }:
            return tool_result, pre_resolved, None
        return None, pre_resolved, str(fallback_code)

    def _competitor_patent_context_call(self, question: str, resolution, *, timing: Timing | None = None) -> ExternalCall | None:
        if self.query_layer is None or not _asks_competitor_ingredients(question):
            return None
        try:
            with stage(timing, "tool:competitor_molecule_candidates", resolution.canonical_brand):
                candidates = self.query_layer.competitor_molecule_candidates(resolution.canonical_brand, limit=5)
        except (LookupError, TypeError, ValueError):
            candidates = []
        nested: list[dict[str, Any]] = []
        anchor_set = {molecule.casefold() for molecule in resolution.molecule_en if molecule}
        for candidate in candidates:
            molecule = str(candidate.get("molecule") or "").strip()
            if not molecule or molecule.casefold() in anchor_set:
                continue
            with stage(timing, "tool:mfds_patent", f"competitor:{molecule}"):
                nested.append(asdict(self.external.mfds_patent(molecule)))
            with stage(timing, "tool:mfds_fda_orangebook", f"competitor:{molecule}"):
                nested.append(asdict(self.external.mfds_fda_orangebook(molecule)))
        status = "ok" if candidates else "no_data"
        return ExternalCall(
            tool="search_patent",
            source="external_api",
            status=status,
            summary_text=f"{resolution.canonical_brand} 경쟁 성분 후보 {len(candidates)}건의 특허 조회 범위를 표시합니다.",
            render_data={
                "status": status,
                "brand": resolution.canonical_brand,
                "competitor_ingredient_candidates": candidates,
                "competitor_patent_coverage": {
                    "status": "attempted" if candidates else "no_candidate",
                    "message": "경쟁 성분 후보별 MFDS/OrangeBook 조회를 시도했습니다." if candidates else "같은 시장 경쟁 성분 후보를 mart에서 확인하지 못했습니다.",
                    "sources": "MFDS 의약품특허목록, FDA OrangeBook",
                    "scope": "현재 특허 DB에서 확인되는 항목만 표시하며, 전체 독점권을 단정하지 않습니다.",
                },
                "calls": nested,
            },
        )

    def _unsupported_brand(self, question: str, routes) -> dict[str, Any]:
        if is_hira_disease_question(question):
            return unsupported_hira_interface_result(
                question,
                routes,
                router_diagnostics(self.router),
            )
        return unsupported_brand_result(question, routes, router_diagnostics(self.router))

    def _requested_source_unavailable(self, question: str, resolution, routes, source_trap) -> dict[str, Any]:
        markdown_response = {
            "fact_md": "데이터 미보유",
            "data_md": "",
            "allowed_numbers": (),
            "evidence": (),
            "verification": {"status": "pass", "unexpected_numbers": ()},
        }
        answer = apply_common_unavailable_response(
            question,
            f"{source_trap.label} 데이터는 현재 운영 데이터에 미보유입니다.",
            markdown_response,
        )
        answer = apply_requested_source_trap_gate(question, answer)
        call = {
            "tool": "requested_source_unavailable",
            "source": "cache",
            "status": "unsupported",
            "summary_text": f"{source_trap.label} 데이터는 현재 운영 데이터에 미보유입니다.",
            "render_data": {"requested_source": source_trap.label, "status": "unsupported"},
        }
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "router_diagnostics": router_diagnostics(self.router),
            "tool_calls": [call],
            "answer": answer,
            "markdown_response": markdown_response,
            "sources": ["cache"],
        }

    def _no_data(self, question: str, resolution, routes) -> dict[str, Any]:
        message = "현재 데이터로 답변 불가합니다. Q4 영업 Impact 또는 Q5 포트폴리오·사업성 영역은 P1 POC 데이터 범위 밖입니다."
        proxy_call = self._no_data_proxy_call(resolution)
        if proxy_call is None:
            markdown = MarkdownResponseBuilder().no_data(message)
            return {
                "question": question,
                "resolution": resolution.__dict__,
                "decomposition": [route.__dict__ for route in routes],
                "router_diagnostics": router_diagnostics(self.router),
                "tool_calls": [],
                "answer": markdown.markdown,
                "markdown_response": markdown.to_dict(),
                "sources": ["none"],
            }
        source = str(proxy_call.get("source") or "cache")
        builder = MarkdownResponseBuilder()
        markdown = builder.build(brand=resolution.canonical_brand, calls=[proxy_call], sources=[source])
        interpretation_md = MarkdownResponseBuilder._join(f"## 해석\n\n- {message}", markdown.interpretation_md)
        answer = MarkdownResponseBuilder._join(
            markdown.summary_md,
            interpretation_md,
            markdown.data_md,
            markdown.evidence_md,
            markdown.sources_md,
            markdown.notice_md,
        )
        markdown_response = markdown.to_dict()
        markdown_response["markdown"] = answer
        markdown_response["interpretation_md"] = interpretation_md
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "router_diagnostics": router_diagnostics(self.router),
            "tool_calls": [proxy_call],
            "answer": answer,
            "markdown_response": markdown_response,
            "sources": [source],
        }

    def _no_data_proxy_call(self, resolution) -> dict[str, Any] | None:
        brand = resolution.canonical_brand
        if self.query_layer is not None:
            try:
                return self.query_layer.brand_metric(brand, "sales", "latest")
            except (LookupError, TypeError, ValueError):
                return None
        if self.metrics._mode == "cache" and not self.metrics._legacy_cache_injected:
            return None
        try:
            return self.metrics.get_brand_metric(brand, metric="sales")
        except (LookupError, TypeError, ValueError):
            return None

    def _metric_call(
        self,
        brand: str,
        *,
        metric: str,
        filter_entries: tuple[FilterEntry, ...],
        market: str | None = None,
        prefer_mart: bool = False,
    ) -> dict[str, Any]:
        if self.query_layer is not None:
            try:
                period = _metric_filter_period(filter_entries)
                if period is not None:
                    if market is None:
                        return self.query_layer.brand_metric(brand, metric, period)
                    return self.query_layer.brand_metric(brand, metric, period, market=market)
                if not filter_entries:
                    if market is None:
                        return self.query_layer.brand_metric(brand, metric, "latest")
                    return self.query_layer.brand_metric(brand, metric, "latest", market=market)
                plan = validate_metric_filters(filter_entries)
                if plan.blocks_results:
                    raise LookupError("d2 query-layer rejected the requested filters")
                if plan.relative_range is not None:
                    months = _relative_range_months(plan.relative_range)
                    if metric in {"market_share", "share"}:
                        spec = {
                            "metrics": ["share"],
                            "derive": ["average"],
                            "filters": {"brand": brand, "periods": months},
                        }
                        if market is not None:
                            spec["market"] = market
                        return self.query_layer.query(spec, fallback_brand=brand)

                    spec = {
                        "group_by": ["product", "period"],
                        "metrics": ["sales"],
                        "derive": ["trend"],
                        "filters": {"brand": brand, "periods": months},
                    }
                    if market is not None:
                        spec["market"] = market
                    return self.query_layer.query(spec, fallback_brand=brand)
                dimension = ""
                if plan.channel is not None:
                    dimension = "channel"
                elif plan.level is not None:
                    dimension = _catalog_dimension_for_level(
                        self.query_layer,
                        brand,
                        market,
                        plan.level,
                    )
                if dimension:
                    kwargs = {
                        "source": plan.source or "",
                        "period": plan.period_month or (str(plan.period_year) if plan.period_year else "latest"),
                    }
                    if market is not None:
                        kwargs["market"] = market
                    return self.query_layer.dimension_breakdown(brand, dimension, **kwargs)
                raise LookupError(f"d2 query-layer route does not support filters: {filter_entries!r}")
            except (LookupError, TypeError, ValueError) as exc:
                return _query_failed_metric_call(brand, metric, filter_entries, exc)
        return self.metrics.get_brand_metric(brand, metric=metric, filter_entries=filter_entries)


def _catalog_dimension_for_level(
    query_layer: StrategicQueryLayer,
    brand: str,
    market: str | None,
    level: str,
) -> str:
    normalized = level.casefold().replace(" ", "_")
    if normalized == "brand":
        return "product"
    catalog = query_layer.catalog_for_brand(brand, market=market)
    if normalized in catalog.dimensions:
        return normalized
    structure = catalog.market_structure or {}
    display_axis = str(structure.get("display_axis") or "")
    if "class" in normalized and display_axis in catalog.dimensions:
        return display_axis
    axes = structure.get("axes")
    if isinstance(axes, (list, tuple)):
        for axis in axes:
            if not isinstance(axis, dict):
                continue
            label = str(axis.get("label") or "").casefold().replace(" ", "_")
            key = str(axis.get("key") or "")
            if label == normalized and key in catalog.dimensions:
                return key
    raise LookupError(f"catalog does not expose requested level: {level}")


def _is_single_brand_trend_question(question: str) -> bool:
    if not needs_market_series(question):
        return False
    widening_tokens = ("경쟁", "구도", "상위", "위협", "시장 영향", "시장 탓", "시장 문제", "고유", "아토젯", "비교", "같이", "랑")
    return not any(token in question for token in widening_tokens)


def _metric_filter_period(filter_entries: tuple[FilterEntry, ...]) -> str | None:
    if not filter_entries:
        return None
    plan = validate_metric_filters(filter_entries)
    if plan.channel is not None or plan.level is not None or plan.blocks_results:
        return None
    if plan.period_month is not None:
        return plan.period_month
    if plan.period_year is not None:
        return str(plan.period_year)
    return None


def _relative_range_months(value: str) -> int:
    match = re.fullmatch(r"\s*최근\s*(\d{1,2})\s*(개월|달|년)\s*", value)
    if match is None:
        raise ValueError(f"unsupported relative range: {value}")
    amount = int(match.group(1))
    months = amount * 12 if match.group(2) == "년" else amount
    if not 1 <= months <= 50:
        raise ValueError(f"relative range is outside the supported window: {value}")
    return months


def _query_failed_metric_call(
    brand: str,
    metric: str,
    filter_entries: tuple[FilterEntry, ...],
    error: BaseException,
) -> dict[str, Any]:
    message = "요청한 기간의 지표 조회에 실패했습니다. 데이터가 없다는 뜻은 아니며, 확인되지 않은 수치를 추정하지 않습니다."
    return {
        "source": "strategic_mart",
        "tool": "query_failed",
        "summary_text": message,
        "render_data": {
            "brand": brand,
            "metric": metric,
            "status": "query_failed",
            "message": message,
            "error_type": type(error).__name__,
            "requested_filters": dict(filter_entries),
        },
    }


def _market_ambiguity_result(question: str, resolution: Any) -> dict[str, Any]:
    message = market_ambiguity_message(
        resolution.canonical_brand,
        resolution.market_names or resolution.market_ids,
    )
    return {
        "question": question,
        "resolution": asdict(resolution),
        "decomposition": [{"intent": "market_clarification", "status": "needs_clarification"}],
        "router_diagnostics": {"mode": "deterministic", "scope": "market_ambiguity"},
        "tool_calls": [],
        "answer": message,
        "markdown_response": {"markdown": message, "fact_md": "", "data_md": ""},
        "sources": [],
    }


def _market_membership_mismatch_result(question: str, resolution: Any) -> dict[str, Any]:
    message = market_membership_mismatch_message(
        resolution.canonical_brand,
        resolution.requested_market_name or resolution.requested_market_id or "요청 시장",
        resolution.market_names or resolution.market_ids,
    )
    return {
        "question": question,
        "resolution": asdict(resolution),
        "decomposition": [{"intent": "market_membership_validation", "status": "unsupported"}],
        "router_diagnostics": {
            "mode": "deterministic",
            "scope": "market_membership_mismatch",
            "gate": "brand_market_membership",
            "gate_reason": "explicit_market_outside_brand_memberships",
        },
        "tool_calls": [],
        "answer": message,
        "markdown_response": {"markdown": message, "fact_md": "", "data_md": ""},
        "sources": [],
    }


def _document_resolution() -> BrandResolution:
    return BrandResolution(
        canonical_brand="업로드 문서",
        audit_code="document_context",
        molecule_en=(),
        atc=(),
        edi_code=None,
        item_seq=None,
        is_combo=False,
        support_source="document_context",
    )


def _brand_clarification_result(question: str) -> dict[str, Any]:
    message = "시장 지표를 함께 조회하려면 브랜드 또는 시장을 지정해 주세요."
    return {
        "question": question,
        "resolution": None,
        "decomposition": [{"intent": "brand_clarification", "status": "needs_clarification"}],
        "router_diagnostics": {"mode": "deterministic", "scope": "unresolved_brand"},
        "tool_calls": [],
        "answer": message,
        "markdown_response": {"markdown": message, "fact_md": "", "data_md": ""},
        "sources": [],
    }


def _conversation_fallback(question: str) -> dict[str, Any] | None:
    normalized = re.sub(r"[\s!?.,~]+", " ", question.casefold()).strip()
    if not normalized:
        return None

    answer: str | None = None
    intent = "conversation"
    if normalized in {"안녕", "안녕하세요", "반가워", "반갑습니다", "하이", "hi", "hello"}:
        answer = "안녕하세요! 의약품 시장 분석을 도와드릴게요. 궁금한 브랜드나 시장을 말씀해 주세요."
        intent = "greeting"
    elif normalized in {"고마워", "고맙습니다", "감사해", "감사합니다", "thanks", "thank you"}:
        answer = "도움이 됐다니 다행이에요. 이어서 궁금한 시장이나 브랜드를 말씀해 주세요."
        intent = "thanks"
    elif any(token in normalized for token in ("뭐 할 수", "무엇을 할 수", "어떤 걸 할 수", "기능 알려")):
        answer = (
            "브랜드 매출과 점유율 추이, 경쟁 구도, 임상시험·허가·특허·부작용, 최신 이슈를 확인할 수 있어요. "
            "첨부한 파일의 집계와 비교 분석도 가능합니다."
        )
        intent = "capabilities"
    elif "날씨" in normalized:
        answer = "날씨는 제 분석 범위가 아니에요. 대신 의약품 시장의 브랜드 매출·점유율이나 경쟁 현황은 확인해 드릴 수 있어요."
        intent = "out_of_scope"
    if answer is None:
        return None
    return {
        "question": question,
        "resolution": None,
        "decomposition": [{"intent": intent, "status": "answered_without_data"}],
        "router_diagnostics": {"mode": "deterministic", "scope": intent},
        "tool_calls": [],
        "answer": answer,
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "sources": [],
        "conversation_fallback_ready": True,
    }


def _is_external_tool_agent_candidate(
    routes: list[Any],
    documents: list[Path],
    *,
    question: str,
) -> bool:
    if documents:
        return False
    if is_top_n_intent(question):
        return False
    if (
        configured_routing_mode() is RoutingMode.ENFORCE
        and classify_question(question).source_domain in {"hira", "regulatory", "clinical_trials"}
    ):
        return True
    sources = {source for route in routes for source in route.sources}
    if "external_api" in sources:
        return True
    if sources == {"document"}:
        return True
    return sources == {"none"} and all(route.bq == "UNKNOWN" for route in routes)


def _annotate_external_tool_fallback(payload: dict[str, Any], fallback_code: str | None) -> dict[str, Any]:
    if fallback_code is None:
        return payload
    annotated = dict(payload)
    diagnostics = annotated.get("router_diagnostics")
    normalized = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    normalized["external_tool_agent_fallback_code"] = fallback_code
    annotated["router_diagnostics"] = normalized
    return annotated


def _prefer_mart_metric(support_source: str) -> bool:
    primary_source = support_source.split("+", 1)[0]
    return primary_source in {
        "catalog_membership",
        "catalog_alias",
        "mart_membership",
        "strategic_mart",
        "cache_brands",
    }


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


def _is_known_ingredient_patent_question(question: str) -> bool:
    lower = question.lower()
    asks_patent = "특허" in question or "patent" in lower or "orange" in lower
    return asks_patent and resolve_patent_ingredient_query(question) is not None


def _answer_scope(question: str) -> str | None:
    if _is_single_brand_trend_question(question):
        return "single_brand_trend"
    if _single_brand_focus_question(question):
        return "single_brand_focus"
    return None


def _asks_competitor_ingredients(question: str) -> bool:
    return any(token in question for token in ("경쟁 성분", "경쟁성분", "경쟁 molecule", "경쟁 Molecule", "경쟁 성분의"))
