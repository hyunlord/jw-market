from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from jw_chat_agent_poc.agentic import FilterEntry
from jw_chat_agent_poc.orchestrator.external_notices import external_unavailable_for_missing_molecule, seeded_false_positive_notice
from jw_chat_agent_poc.orchestrator.hira_disease import hira_disease_calls
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
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


class AgentLoopResolution(Protocol):
    canonical_brand: str
    molecule_en: tuple[str, ...]
    is_combo: bool


def search_news_call(news: DeepAnalysisNewsTool, brand: str, query: str) -> dict:
    filters: tuple[FilterEntry, ...] = (("text_contains", query),) if query else ()
    call = news.related_news(brand, filter_entries=filters)
    data = call.setdefault("render_data", {})
    data["facade_tool"] = "search_news"
    data["filter_entries"] = filters
    data["provenance"] = {"source": "events/event_brand_scores", "mode": "full_corpus_or_cache_fallback"}
    return call


def background_news_context_call(news: DeepAnalysisNewsTool, brand: str, relevance_brands: tuple[str, ...] = ()) -> dict:
    filters: tuple[FilterEntry, ...] = ()
    if relevance_brands:
        filters = (
            ("relevance_brands", "|".join(relevance_brands)),
            ("relevance_operator", "OR"),
        )
    call = news.related_news(brand, limit=3, filter_entries=filters)
    data = call.setdefault("render_data", {})
    data["facade_tool"] = "background_news_context"
    data["context_role"] = "background_insight"
    data["filter_entries"] = filters
    data["provenance"] = {"source": "events/event_brand_scores", "mode": "full_corpus_or_cache_fallback"}
    return call


def disease_stats_call(question: str, resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = hira_disease_calls(question, resolution, external)
    return _aggregate_call(
        facade_tool="get_disease_stats",
        source="hira_disease",
        status=_aggregate_status(calls),
        calls=calls,
        summary_prefix=f"{resolution.canonical_brand} HIRA 질병 통계",
    )


def clinical_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = _clinical_calls(resolution, external)
    return _aggregate_call("search_clinical", "external_api", _aggregate_status(calls), calls, f"{resolution.canonical_brand} 임상 근거")


def patent_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = _patent_calls(resolution, external)
    return _aggregate_call("search_patent", "external_api", _aggregate_status(calls), calls, f"{resolution.canonical_brand} 특허 근거")


def _clinical_calls(resolution: AgentLoopResolution, external: ExternalApiClient) -> list[ExternalCall]:
    if not resolution.molecule_en:
        return [external_unavailable_for_missing_molecule(resolution)]
    if is_external_inapplicable_brand(resolution.canonical_brand):
        return [inapplicable_call(resolution.canonical_brand, resolution.molecule_en)]
    calls: list[ExternalCall] = []
    if resolution.is_combo:
        calls.append(
            annotate_clinical_call(
                external.clinicaltrials_v2_search(combo_query(resolution.molecule_en)),
                resolution.canonical_brand,
                resolution.molecule_en,
                "combo_and",
            )
        )
        for molecule in resolution.molecule_en:
            calls.append(
                annotate_clinical_call(
                    external.clinicaltrials_v2_search(molecule),
                    resolution.canonical_brand,
                    (molecule,),
                    "component_reference",
                )
            )
    else:
        calls.append(
            annotate_clinical_call(
                external.clinicaltrials_v2_search(" OR ".join(resolution.molecule_en)),
                resolution.canonical_brand,
                resolution.molecule_en,
                "molecule_trend",
            )
        )
    calls.append(external.mfds_clinical_trial_kr(resolution.canonical_brand))
    calls.append(clinical_scope_notice(resolution.canonical_brand, resolution.molecule_en, resolution.is_combo).to_call())
    if needs_seeded_false_positive_filter(resolution.canonical_brand):
        calls.append(seeded_false_positive_notice(resolution))
    return calls


def _patent_calls(resolution: AgentLoopResolution, external: ExternalApiClient) -> list[ExternalCall]:
    if not resolution.molecule_en:
        return [external_unavailable_for_missing_molecule(resolution)]
    if is_external_inapplicable_brand(resolution.canonical_brand):
        return [inapplicable_call(resolution.canonical_brand, resolution.molecule_en)]
    calls: list[ExternalCall] = []
    for molecule in resolution.molecule_en:
        calls.append(external.mfds_patent(molecule))
        calls.append(external.mfds_fda_orangebook(molecule))
    calls.append(label_patent_scope_notice(resolution.canonical_brand, resolution.molecule_en).to_call())
    return calls


def _aggregate_call(facade_tool: str, source: str, status: str, calls: list[ExternalCall], summary_prefix: str) -> dict:
    detail = [asdict(call) for call in calls]
    return {
        "source": source,
        "tool": facade_tool,
        "status": status,
        "summary_text": f"{summary_prefix}: " + " / ".join(call.summary_text for call in calls[:3]),
        "render_data": {
            "status": status,
            "facade_tool": facade_tool,
            "calls": detail,
            "fact_count": len(calls),
            "provenance": {"source": source, "tools": [call.tool for call in calls]},
        },
    }


def _aggregate_status(calls: list[ExternalCall]) -> str:
    if any(call.status in {"unsupported", "inapplicable", "error", "no_data"} for call in calls):
        return "partial" if len(calls) > 1 else calls[0].status
    return "ok"
