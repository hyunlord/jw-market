from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from jw_chat_agent_poc.agent_loop import ToolUseAgent, should_use_agent_loop
from jw_chat_agent_poc.agent_loop.factory import (
    ChatAgentDependencyOverrides,
    build_chat_agent_dependencies,
    build_tool_use_agent,
    unsupported_brand_result,
)
from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.agentic import FilterEntry, relevance_filter_entries, relevance_question_text, validate_metric_filters
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.orchestrator.external_notices import (
    external_unavailable_for_missing_molecule,
    seeded_false_positive_notice,
)
from jw_chat_agent_poc.orchestrator.hira_disease import HIRA_DISEASE_MAPPINGS, hira_disease_calls, is_hira_disease_question
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.question_intent import allows_background_news_context, metric_from_question, requires_brand
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.common.timing import Timing, new_timing, stage
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate, requested_unavailable_source
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
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
        with stage(timing, "question_decomposition", "BQ and tool routing"):
            routes = self.router.route(question, has_documents=bool(docs))
        if not docs and _is_known_ingredient_patent_question(question):
            loop = self.agent_loop or build_tool_use_agent(self._agent_loop_dependencies)
            return loop.answer(question)
        requires_brand_flag = requires_brand(routes) and not is_hira_disease_question(question)
        portfolio_scope = not docs and is_portfolio_decline_question(question, routes) and should_use_agent_loop(question)
        try:
            with stage(timing, "agent_pre_resolve", "brand resolver"):
                resolution = self.resolver.resolve(question, allow_default=portfolio_scope or bool(docs) or not requires_brand_flag)
        except UnsupportedBrandError:
            return self._unsupported_brand(question, routes)
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        sources: list[str] = []
        source_trap = requested_unavailable_source(question)
        if source_trap is not None and not docs:
            return self._requested_source_unavailable(question, resolution, routes, source_trap)

        if not docs and should_use_agent_loop(question):
            loop = self.agent_loop or build_tool_use_agent(self._agent_loop_dependencies)
            return loop.answer(question)

        if any("none" in route.sources for route in routes):
            return self._no_data(question, resolution, routes)

        if any("deep_analysis_events" in route.sources for route in routes):
            news_filters = tuple(entry for route in routes if "deep_analysis_events" in route.sources for entry in route.filters)
            news_brands = self._news_brands(question, routes, resolution.canonical_brand)
            news_filters = (*news_filters, *relevance_filter_entries(news_brands, question))
            call = self.news.related_news(news_brands[0], filter_entries=news_filters)
            calls.append(call)
            sources.append(call["source"])

        if any("metrics" in route.sources for route in routes):
            market = resolution.market_id or ("ml_006" if resolution.canonical_brand in {"리바로", "리바로젯"} else "mock_market")
            metric = metric_from_question(question)
            metric_filters = tuple(entry for route in routes if "metrics" in route.sources for entry in route.filters)
            filter_plan = validate_metric_filters(metric_filters)
            effective_filters = metric_filters if filter_plan.has_effective_filter else ()
            with stage(timing, "tool:get_brand_metric", f"metric={metric}"):
                brand_metric_call = self._metric_call(
                    resolution.canonical_brand,
                    metric=metric,
                    filter_entries=effective_filters,
                    prefer_mart=resolution.support_source == "mart_membership",
                )
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
                resolution.support_source != "mart_membership"
                and not effective_filters
                and metric not in {"hhi", "series", "trend", "momentum", "ei"}
            ):
                with stage(timing, "tool:get_market_landscape", f"market={market}"):
                    market_landscape_call = self.metrics.get_market_landscape(market)
                metric_calls.insert(0, market_landscape_call)
            for call in metric_calls:
                calls.append(call)
                sources.append(call["source"])

        if self._should_attach_background_news(question, calls):
            call = self.news.related_news(resolution.canonical_brand, limit=3)
            data = call.setdefault("render_data", {})
            data["facade_tool"] = "background_news_context"
            data["context_role"] = "background_insight"
            data["provenance"] = {"source": "events/event_brand_scores", "mode": "full_corpus_or_cache_fallback"}
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
            rag_result = self.rag.search(question, docs)
            calls.append({"tool": "document_rag", **rag_result.__dict__})
            sources.append(rag_result.source)

        with stage(timing, "fact_assembly", "markdown fact set build"):
            markdown = MarkdownResponseBuilder().build(
                brand=resolution.canonical_brand,
                calls=calls,
                sources=sources,
                notices=notices,
            )
            answer = apply_requested_source_trap_gate(question, markdown.markdown)
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "router_diagnostics": router_diagnostics(self.router),
            "tool_calls": calls,
            "answer": answer,
            "markdown_response": markdown.to_dict(),
            "sources": sorted(set(sources)),
            "timing": timing,
        }

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
                pass
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
        prefer_mart: bool = False,
    ) -> dict[str, Any]:
        if self.query_layer is not None:
            try:
                if prefer_mart and not filter_entries:
                    return self.query_layer.brand_metric(brand, metric, "latest")
                catalog = self.query_layer.catalog_for_brand(brand)
                if catalog.market_structure:
                    period = _metric_filter_period(filter_entries)
                    if period is not None:
                        return self.query_layer.brand_metric(brand, metric, period)
                    if not filter_entries:
                        return self.query_layer.brand_metric(brand, metric, "latest")
            except (LookupError, TypeError, ValueError):
                pass
        return self.metrics.get_brand_metric(brand, metric=metric, filter_entries=filter_entries)


def _is_single_brand_trend_question(question: str) -> bool:
    if "매출" not in question or not any(token in question for token in ("추이", "변화", "증감", "하락", "감소", "줄")):
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
