from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Final, Protocol

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


_MFDS_PRODUCT_FORM_PREFIXES: Final[tuple[str, ...]] = (
    "정",
    "캡슐",
    "주",
    "시럽",
    "액",
    "산",
    "과립",
    "겔",
    "크림",
    "연고",
    "패치",
    "서방",
    "구강",
    "점안",
    "흡입",
    "현탁",
    "프리필드",
)


def search_news_call(news: DeepAnalysisNewsTool, brand: str, query: str) -> dict:
    filters: tuple[FilterEntry, ...] = (("text_contains", query),) if query else ()
    call = news.related_news(brand, filter_entries=filters)
    data = call.setdefault("render_data", {})
    data["facade_tool"] = "search_news"
    data["filter_entries"] = filters
    data["provenance"] = {"source": "events/event_brand_scores", "mode": "corpus_only"}
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
    data["provenance"] = {"source": "events/event_brand_scores", "mode": "corpus_only"}
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


def procedure_stats_call(question: str, resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    st5_cd = _procedure_code(question)
    if not st5_cd:
        call = ExternalCall(
            tool="hira_procedure_code_unresolved",
            source="hira_procedure",
            status="unsupported",
            summary_text="진료행위 통계 조회에는 HIRA 5단 행위코드(st5Cd)가 필요합니다.",
            render_data={
                "brand": resolution.canonical_brand,
                "message": "HIRA 5단 행위코드(st5Cd) 미확인",
                "required_fields": ["st5Cd", "year", "stdType"],
            },
        )
        calls = [call]
    else:
        year = _procedure_year(question)
        calls = [
            external.hira_procedure_gender_ipat_opat_stats(st5_cd, year=year),
            external.hira_procedure_gender_age_stats(st5_cd, year=year),
            external.hira_procedure_institution_class_stats(st5_cd, year=year),
            external.hira_procedure_area_stats(st5_cd, year=year),
        ]
    return _aggregate_call(
        facade_tool="get_procedure_stats",
        source="hira_procedure",
        status=_aggregate_status(calls),
        calls=calls,
        summary_prefix=f"{resolution.canonical_brand} HIRA 진료행위 통계",
    )


def clinical_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = _filter_clinical_calls_to_resolution(_clinical_calls(resolution, external), resolution)
    return _aggregate_call("search_clinical", "external_api", _aggregate_status(calls), calls, f"{resolution.canonical_brand} 임상 근거")


def patent_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = _patent_calls(resolution, external)
    return _aggregate_call("search_patent", "external_api", _aggregate_status(calls), calls, f"{resolution.canonical_brand} 특허 근거")


def patent_ingredient_call(ingredient_en: str, external: ExternalApiClient) -> dict:
    calls = [external.mfds_patent(ingredient_en), external.mfds_fda_orangebook(ingredient_en)]
    return _aggregate_call("search_patent", "external_api", _aggregate_status(calls), calls, f"{ingredient_en} 특허 근거")


def drug_info_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    calls = _drug_info_calls(resolution, external)
    return _aggregate_call("search_drug_info", "external_api", _aggregate_status(calls), calls, f"{resolution.canonical_brand} MFDS 허가정보")


def safety_call(resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    if not resolution.molecule_en:
        calls = [external_unavailable_for_missing_molecule(resolution)]
    elif is_external_inapplicable_brand(resolution.canonical_brand):
        calls = [inapplicable_call(resolution.canonical_brand, resolution.molecule_en)]
    elif resolution.is_combo:
        calls = [external.openfda_combo_label_search(resolution.molecule_en)]
        calls.extend(external.openfda_label_search(molecule) for molecule in resolution.molecule_en)
    else:
        calls = [external.openfda_label_search(molecule) for molecule in resolution.molecule_en]
    return _aggregate_call(
        "search_safety",
        "external_api",
        _aggregate_status(calls),
        calls,
        f"{resolution.canonical_brand} FDA 안전성 근거",
    )


def web_search_call(question: str, resolution: AgentLoopResolution, external: ExternalApiClient) -> dict:
    query = _web_search_query(question, resolution)
    call = external.web_search(query)
    return _aggregate_call("web_search", "web_search", call.status, [call], f"{resolution.canonical_brand} 웹 검색 결과")


def _procedure_code(question: str) -> str:
    import re

    match = re.search(r"(?<![A-Za-z0-9])([A-Z]{1,3}\d{2,5})(?![A-Za-z0-9])", question.upper())
    return match.group(1) if match else ""


def _procedure_year(question: str) -> str:
    import re

    match = re.search(r"(20\d{2})", question)
    return match.group(1) if match else "2024"


def _web_search_query(question: str, resolution: AgentLoopResolution) -> str:
    terms: list[str] = []
    if resolution.canonical_brand:
        terms.append(resolution.canonical_brand)
    terms.extend(molecule for molecule in resolution.molecule_en if molecule)
    terms.extend(("제약", "의약품"))
    terms.append(question)
    deduped = tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))
    return " ".join(deduped)


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


def _filter_clinical_calls_to_resolution(calls: list[ExternalCall], resolution: AgentLoopResolution) -> list[ExternalCall]:
    return [_filter_mfds_clinical_call(call, resolution) for call in calls]


def _filter_mfds_clinical_call(call: ExternalCall, resolution: AgentLoopResolution) -> ExternalCall:
    if call.tool != "mfds_clinical_trial_kr":
        return call
    raw_items = call.render_data.get("items")
    if not isinstance(raw_items, list):
        return call
    tokens = _clinical_match_tokens(resolution)
    filtered = [item for item in raw_items if isinstance(item, dict) and _clinical_item_matches(item, tokens)]
    if filtered:
        data = dict(call.render_data)
        data["items"] = filtered
        data["filtered_from_count"] = len(raw_items)
        return replace(call, render_data=data, summary_text=f"{resolution.canonical_brand} MFDS 임상시험 {len(filtered)}건을 근거로 사용합니다.")
    data = {
        "status": "no_data",
        "brand": resolution.canonical_brand,
        "items": [],
        "filtered_from_count": len(raw_items),
        "message": f"{resolution.canonical_brand} 또는 성분 기준으로 매칭되는 MFDS 임상 row 없음",
    }
    return replace(
        call,
        status="no_data",
        summary_text=f"{resolution.canonical_brand} MFDS 임상시험은 broad 결과 {len(raw_items)}건을 제외해 근거 생성 안 함.",
        render_data=data,
    )


def _clinical_match_tokens(resolution: AgentLoopResolution) -> tuple[str, ...]:
    values = [resolution.canonical_brand, *resolution.molecule_en]
    return tuple(value.casefold() for value in values if value)


def _clinical_item_matches(item: dict[str, Any], tokens: tuple[str, ...]) -> bool:
    text = " ".join(str(value) for value in item.values() if isinstance(value, str)).casefold()
    return any(token in text for token in tokens)


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


def _drug_info_calls(resolution: AgentLoopResolution, external: ExternalApiClient) -> list[ExternalCall]:
    brand = resolution.canonical_brand
    search = external.mfds_permission_search(brand)
    item = _first_matching_mfds_item(search, brand)
    if item is None:
        return [search, _mfds_no_data("mfds_permission_search", brand, "MFDS 허가 품목 검색에서 브랜드 일치 결과를 확인하지 못했습니다.")]
    item_seq = str(item.get("ITEM_SEQ") or "").strip()
    if not item_seq:
        return [search, _mfds_no_data("mfds_permission_search", brand, "MFDS 허가 품목 검색 결과에 ITEM_SEQ가 없어 상세 조회를 보류했습니다.")]
    detail = external.mfds_permission_detail(item_seq)
    if not _has_mfds_items(detail):
        return [search, _mfds_no_data("mfds_permission_detail", brand, "MFDS 허가 상세 조회 결과가 없어 허가정보 생성을 보류했습니다.")]
    return [search, detail]


def _first_matching_mfds_item(call: ExternalCall, brand: str) -> dict[str, Any] | None:
    if not _has_mfds_items(call):
        return None
    brand_key = _normal_key(brand)
    for item in _mfds_items(call):
        name = _normal_key(item.get("ITEM_NAME") or item.get("itemName") or "")
        if _is_mfds_product_family_match(name, brand_key):
            return item
    return None


def _is_mfds_product_family_match(product_name: str, canonical_brand: str) -> bool:
    if not canonical_brand or not product_name.startswith(canonical_brand):
        return False
    suffix = product_name[len(canonical_brand) :]
    if not suffix:
        return True
    if suffix[0].isdigit() or suffix[0] in "(-[/":
        return True
    return suffix.startswith(_MFDS_PRODUCT_FORM_PREFIXES)


def _has_mfds_items(call: ExternalCall) -> bool:
    if call.status in {"error", "no_data", "unsupported", "inapplicable"}:
        return False
    data = call.render_data
    result_code = str(data.get("resultCode") or data.get("RESULT_CODE") or "00")
    if result_code != "00":
        return False
    return bool(_mfds_items(call))


def _mfds_items(call: ExternalCall) -> list[dict[str, Any]]:
    raw_items = call.render_data.get("items")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _normal_key(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _mfds_no_data(tool: str, brand: str, message: str) -> ExternalCall:
    return ExternalCall(
        tool=tool,
        source="external_api",
        status="no_data",
        summary_text=f"{brand} {message} 근거 생성 안 함.",
        render_data={"status": "no_data", "brand": brand, "message": message, "items": []},
    )


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
