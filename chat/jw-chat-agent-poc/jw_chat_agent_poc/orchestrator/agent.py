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
from jw_chat_agent_poc.agentic import relevance_filter_entries, relevance_question_text, validate_metric_filters
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.orchestrator.external_notices import (
    external_unavailable_for_missing_molecule,
    seeded_false_positive_notice,
)
from jw_chat_agent_poc.orchestrator.hira_disease import HIRA_DISEASE_MAPPINGS, hira_disease_calls, is_hira_disease_question
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.question_intent import allows_background_news_context, metric_from_question, requires_brand
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.router import BQRouter, LLMFirstBQRouter
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
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
        docs = documents or []
        routes = self.router.route(question, has_documents=bool(docs))
        requires_brand_flag = requires_brand(routes) and not is_hira_disease_question(question)
        portfolio_scope = not docs and is_portfolio_decline_question(question, routes) and should_use_agent_loop(question)
        try:
            resolution = self.resolver.resolve(question, allow_default=portfolio_scope or bool(docs) or not requires_brand_flag)
        except UnsupportedBrandError:
            return self._unsupported_brand(question, routes)
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        sources: list[str] = []

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
            metric_calls = [
                self.metrics.get_brand_metric(
                    resolution.canonical_brand,
                    metric=metric,
                    filter_entries=effective_filters,
                )
            ]
            scope = _answer_scope(question)
            if scope is not None:
                for metric_call in metric_calls:
                    data = metric_call.get("render_data")
                    if not isinstance(data, dict):
                        continue
                    if scope == "single_brand_trend" and data.get("metric") not in {"series", "trend"}:
                        continue
                    data["answer_scope"] = scope
            if not effective_filters and metric not in {"hhi", "series", "trend", "momentum", "ei"}:
                metric_calls.insert(0, self.metrics.get_market_landscape(market))
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
            external_calls = self._external_calls(question, resolution)
            for call in external_calls:
                calls.append(call.__dict__)
                if call.tool == "matching_policy_notice":
                    notices.append(call.summary_text)
                sources.append(call.source)

        if docs and any("document" in route.sources for route in routes):
            rag_result = self.rag.search(question, docs)
            calls.append({"tool": "document_rag", **rag_result.__dict__})
            sources.append(rag_result.source)

        markdown = MarkdownResponseBuilder().build(
            brand=resolution.canonical_brand,
            calls=calls,
            sources=sources,
            notices=notices,
        )
        return {
            "question": question,
            "resolution": resolution.__dict__,
            "decomposition": [route.__dict__ for route in routes],
            "router_diagnostics": router_diagnostics(self.router),
            "tool_calls": calls,
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": sorted(set(sources)),
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

    def _external_calls(self, question: str, resolution) -> list[ExternalCall]:
        lower = question.lower()
        calls: list[ExternalCall] = []
        if is_hira_disease_question(question):
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
                calls.append(
                    annotate_clinical_call(
                        self.external.clinicaltrials_v2_search(combo_query(resolution.molecule_en)),
                        resolution.canonical_brand,
                        resolution.molecule_en,
                        "combo_and",
                    )
                )
                for molecule in resolution.molecule_en:
                    calls.append(
                        annotate_clinical_call(
                            self.external.clinicaltrials_v2_search(molecule),
                            resolution.canonical_brand,
                            (molecule,),
                            "component_reference",
                        )
                    )
            else:
                calls.append(
                    annotate_clinical_call(
                        self.external.clinicaltrials_v2_search(" OR ".join(resolution.molecule_en)),
                        resolution.canonical_brand,
                        resolution.molecule_en,
                        "molecule_trend",
                    )
                )
            calls.append(self.external.mfds_clinical_trial_kr(resolution.canonical_brand))
            calls.append(clinical_scope_notice(resolution.canonical_brand, resolution.molecule_en, resolution.is_combo).to_call())
            if needs_seeded_false_positive_filter(resolution.canonical_brand):
                calls.append(seeded_false_positive_notice(resolution))
        if "fda" in lower or "라벨" in question or "label" in lower:
            if resolution.is_combo:
                calls.append(self.external.openfda_combo_label_search(resolution.molecule_en))
            for molecule in resolution.molecule_en:
                calls.append(self.external.openfda_label_search(molecule))
        if "특허" in question or "patent" in lower or "orange" in lower:
            for molecule in resolution.molecule_en:
                calls.append(self.external.mfds_patent(molecule))
                calls.append(self.external.mfds_fda_orangebook(molecule))
            calls.append(label_patent_scope_notice(resolution.canonical_brand, resolution.molecule_en).to_call())
            competitor_context = self._competitor_patent_context_call(question, resolution)
            if competitor_context is not None:
                calls.append(competitor_context)
        if not calls:
            calls.append(self.external.mfds_permission_search(resolution.canonical_brand))
        return calls

    def _competitor_patent_context_call(self, question: str, resolution) -> ExternalCall | None:
        if self.query_layer is None or not _asks_competitor_ingredients(question):
            return None
        try:
            candidates = self.query_layer.competitor_molecule_candidates(resolution.canonical_brand, limit=5)
        except (LookupError, TypeError, ValueError):
            candidates = []
        nested: list[dict[str, Any]] = []
        anchor_set = {molecule.casefold() for molecule in resolution.molecule_en if molecule}
        for candidate in candidates:
            molecule = str(candidate.get("molecule") or "").strip()
            if not molecule or molecule.casefold() in anchor_set:
                continue
            nested.append(asdict(self.external.mfds_patent(molecule)))
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

    def _no_data(self, question: str, resolution, routes) -> dict[str, Any]:
        markdown = MarkdownResponseBuilder().no_data(
            "현재 데이터로 답변 불가합니다. Q4 영업 Impact 또는 Q5 포트폴리오·사업성 영역은 P1 POC 데이터 범위 밖입니다."
        )
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


def _is_single_brand_trend_question(question: str) -> bool:
    if "매출" not in question or not any(token in question for token in ("추이", "변화", "증감", "하락", "감소", "줄")):
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


def _answer_scope(question: str) -> str | None:
    if _is_single_brand_trend_question(question):
        return "single_brand_trend"
    if _single_brand_focus_question(question):
        return "single_brand_focus"
    return None


def _asks_competitor_ingredients(question: str) -> bool:
    return any(token in question for token in ("경쟁 성분", "경쟁성분", "경쟁 molecule", "경쟁 Molecule", "경쟁 성분의"))
