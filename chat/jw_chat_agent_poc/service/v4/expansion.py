from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from jw_chat_agent_poc.hira_catalog import (
    catalog_parent_codes_for_name,
    select_catalog_population,
)
from jw_chat_agent_poc.hira_surface import filter_hira_codes, hira_disease_mapping
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    QueryScope,
    SourceResult,
    tool_query_sources,
)
from jw_chat_agent_poc.service.v4.entity_registry import (
    DiseaseEntity,
    brand_molecule_records,
    resolve_disease_entity,
)
from jw_chat_agent_poc.service.v4.query_scope import (
    configured_entity_limit,
    disease_brand_set,
    disease_kcd_codes,
    strip_mapped_disease_names,
)

_KCD_RANGE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<prefix>[A-Z])(?P<start>\d{2})\s*(?:~|～|부터|[-–—])\s*"
    r"(?:(?P=prefix))?(?P<end>\d{2})(?![A-Z0-9])",
    re.IGNORECASE,
)
_KCD_SINGLE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<code>[A-Z]\d{2}(?:[._]?\d{1,2})?)(?![A-Z0-9])",
    re.IGNORECASE,
)
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)((?:20)?\d{2})\s*년(?:도)?")
_YEAR_RANGE_RE = re.compile(
    r"(?<!\d)(?P<start>(?:20)?\d{2})\s*년(?:도)?\s*"
    r"(?:부터|~|～|[-–—])\s*"
    r"(?P<end>(?:20)?\d{2})\s*년(?:도)?"
)
_RECENT_YEARS_RE = re.compile(r"최근\s*(?P<count>\d{1,2})\s*(?:개)?년")
_PRODUCT_KEYS = frozenset(
    {
        "item_name",
        "product_name",
        "product",
        "brand",
        "brand_name",
        "품목명",
        "제품명",
        "브랜드",
    }
)
_QUERY_SUFFIX = {
    "mart": "내부 시장 데이터",
    "nedrug": "허가 정보",
    "hira": "급여 및 환자 통계",
    "openfda": "미국 허가 및 안전성",
    "clinicaltrials": "임상현황",
    "web": "공개 자료",
    "patent": "특허현황",
}

# The dictionary can turn one disease molecule into many brand/market axes.
# Keep that deterministic fan-out bounded separately from the source-wide cap.
DISEASE_MART_FANOUT_LIMIT = 8
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpansionOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]


def _mart_year_axis_is_redundant(plan: PlannerOutput) -> bool:
    """True when explicit period bounds already decide the mart history span.

    ``_strategic_mart_calls`` derives its span from ``period_from``/``period_to``
    and only falls back to reading a year out of the query text when no bound is
    given. With both bounds present the year suffix changes nothing, so the year
    variants are duplicates; without them it still selects the period and must
    be kept.
    """
    shape = plan.requested_answer_shape
    return bool(shape.period_from) and bool(shape.period_to)


def expand_parameter_axes(
    plan: PlannerOutput,
    question: str,
    *,
    observed_on: date,
    molecule_reader: Any | None = None,
    disease_code_client: Any | None = None,
) -> ExpansionOutcome:
    interpreted = " ".join(
        (
            question,
            plan.resolved_question,
            *plan.expanded_intents,
            *plan.requested_answer_shape.entities,
        )
    )
    question_codes = _kcd_codes(question)
    mcp_lookup: dict[str, Any] = {"status": "not_needed"}
    mcp_codes: tuple[str, ...] = ()
    mcp_is_authoritative = False
    if not question_codes and getattr(disease_code_client, "mode", None) == "live":
        mcp_codes, mcp_lookup = _mcp_disease_codes(
            question,
            interpreted,
            disease_code_client,
        )
        if mcp_lookup.get("status") in {"failed", "no_codes"}:
            retry_codes, retry_lookup = _mcp_disease_codes(
                question,
                interpreted,
                disease_code_client,
                prefer_cache=False,
            )
            mcp_lookup["retry"] = retry_lookup
            if retry_codes:
                mcp_codes = retry_codes
                mcp_lookup["status"] = "resolved"
                mcp_lookup["selected_count"] = len(retry_codes)
            else:
                mcp_lookup["status"] = retry_lookup.get(
                    "status",
                    mcp_lookup["status"],
                )
        mcp_is_authoritative = mcp_lookup.get("status") != "not_applicable"
    interpreted_codes = (
        _kcd_codes(interpreted)
        if not question_codes and not mcp_is_authoritative
        else ()
    )
    dictionary_codes = (
        disease_kcd_codes(interpreted)
        if not question_codes and not interpreted_codes and not mcp_is_authoritative
        else ()
    )
    if question_codes:
        codes = question_codes
        kcd_source = "question_regex"
    elif mcp_is_authoritative:
        codes = mcp_codes
        kcd_source = "hira_disease_name_code" if codes else "unresolved"
    elif interpreted_codes:
        codes = interpreted_codes
        kcd_source = "interpreted_regex"
    elif dictionary_codes:
        codes = dictionary_codes
        kcd_source = "disease_kcd_sets"
    else:
        codes, mcp_lookup = _mcp_disease_codes(question, interpreted, disease_code_client)
        kcd_source = "hira_disease_name_code" if codes else "unresolved"
    if kcd_source != "hira_disease_name_code":
        codes = codes[:configured_entity_limit()]
    # The user's relative period is authoritative when a planner also emits a
    # stale calendar expansion such as "recent 5 years (2021-2025)".
    years = _years(question, observed_on)
    if not years and kcd_source not in {"hira_disease_name_code", "unresolved"}:
        years = _years(interpreted, observed_on)
    updates: dict[str, tuple[str, ...]] = {}
    expansion_omitted: dict[str, tuple[str, ...]] = {}
    unexecuted_reasons: dict[str, str] = {}
    entity_expansion: dict[str, Any] = {"status": "not_applicable"}
    plan_updates: dict[str, Any] = {}
    disease_entity = resolve_disease_entity(question)
    if codes:
        base = strip_mapped_disease_names(_query_subject(question, codes, years)) or "환자수"
        if years:
            updates["hira"] = tuple(
                f"{code} {base} {year}년" for code in codes for year in years
            )
        else:
            updates["hira"] = tuple(f"{code} {base}" for code in codes)
    elif kcd_source == "unresolved" and mcp_lookup.get("status") != "not_applicable":
        updates["hira"] = ()
        unexecuted_reasons["hira"] = (
            "disease_code_lookup_failed"
            if mcp_lookup.get("status") == "failed"
            else "disease_code_unresolved"
        )
    elif len(years) > 1:
        for source in tool_query_sources(plan.answer_sources):
            queries = getattr(plan.tool_queries, source)
            if source == "mart" and _mart_year_axis_is_redundant(plan):
                # The mart lane reads its history span from the period bounds and
                # ignores a trailing year once those bounds are set, so the year
                # variants of one query issue byte-identical calls. Multiplying
                # them only splits the retrieval budget across duplicates.
                updates[source] = tuple(
                    dict.fromkeys(_strip_years(query).strip() for query in queries)
                )
                continue
            updates[source] = tuple(
                dict.fromkeys(
                    f"{_strip_years(query)} {year}년".strip()
                    for query in queries
                    for year in years
                )
            )
    disease_axis: dict[str, Any] = {"status": "not_applicable"}
    if disease_entity is not None:
        policy_updates, disease_axis, policy_reasons = _disease_lane_policy(
            question,
            disease_entity,
        )
        updates.update(policy_updates)
        unexecuted_reasons.update(policy_reasons)
        resolved = tuple(
            dict.fromkeys(
                (
                    *plan.requested_answer_shape.entities,
                    disease_entity.canonical_name,
                    f"condition:{disease_entity.condition}",
                    *(f"ingredient:{value}" for value in disease_axis["treatments"]),
                )
            )
        )
        plan_updates["requested_answer_shape"] = plan.requested_answer_shape.model_copy(
            update={"entities": resolved}
        )
        plan_updates["answer_contract"] = plan.answer_contract.model_copy(
            update={"resolved_entities": resolved}
        )

    anchor_brands, brand_source = disease_brand_set(interpreted)
    anchor_brands = anchor_brands[: configured_entity_limit()]
    molecule_expansion = None if disease_entity is not None else _expand_disease_molecules(
        plan,
        question,
        codes=codes,
        seed_brands=anchor_brands,
        molecule_reader=molecule_reader,
    )
    if molecule_expansion is not None:
        for source, queries in molecule_expansion["requests"].items():
            updates[source] = tuple(
                dict.fromkeys((*updates.get(source, ()), *queries))
            )
        entity_expansion = {
            key: value
            for key, value in molecule_expansion.items()
            if key != "requests"
        }
        entity_expansion["requests"] = {
            source: list(queries)
            for source, queries in molecule_expansion["requests"].items()
        }
        expansion_omitted = {
            source: tuple(queries)
            for source, queries in molecule_expansion.get("omitted_requests", {}).items()
            if queries
        }
    if anchor_brands:
        patent_queries = tuple(f"{brand} 특허현황" for brand in anchor_brands)
        if "특허" in question:
            updates["patent"] = patent_queries
            updates["mart"] = tuple(
                f"{brand} 내부 시장 데이터" for brand in anchor_brands
            )
            expansion_omitted.pop("mart", None)
            entity_expansion = {
                "status": "expanded",
                "source": "query_expansion_data",
                "source_detail": brand_source,
                "entities": list(anchor_brands),
                "requests": {
                    "patent": list(patent_queries),
                    "mart": list(updates["mart"]),
                },
            }
        else:
            entity_expansion.update(
                {
                    "status": "expanded",
                    "source_detail": brand_source,
                    "fixture_brands": list(anchor_brands),
                    "display_scope": "query_only",
                }
            )
    if updates:
        plan_updates["tool_queries"] = plan.tool_queries.model_copy(update=updates)
    if expansion_omitted or unexecuted_reasons:
        previous = plan.query_scope
        requested = dict(previous.requested_calls) if previous else {}
        executed = dict(previous.executed_calls) if previous else {}
        omitted = dict(previous.omitted_queries) if previous else {}
        reasons = dict(previous.unexecuted_reasons) if previous else {}
        for source, skipped in expansion_omitted.items():
            selected = updates.get(source, getattr(plan.tool_queries, source))
            requested[source] = max(requested.get(source, 0), len(selected) + len(skipped))
            executed[source] = len(selected)
            omitted[source] = tuple(dict.fromkeys((*omitted.get(source, ()), *skipped)))
        reasons.update(unexecuted_reasons)
        plan_updates["query_scope"] = QueryScope(
            requested_calls=requested,
            executed_calls=executed,
            omitted_queries=omitted,
            unexecuted_reasons=reasons,
        )
    expanded = (
        plan.model_copy(update=plan_updates)
        if plan_updates
        else plan
    )
    return ExpansionOutcome(
        plan=expanded,
        trace={
            "status": "expanded" if updates else "not_applicable",
            "axes": {"kcd_codes": list(codes), "years": list(years)},
            "kcd_resolution": {"source": kcd_source},
            "mcp_disease_lookup": mcp_lookup,
            "requests": {
                source: list(queries) for source, queries in sorted(updates.items())
            },
            "entity_expansion": entity_expansion,
            "disease_lane_axis": disease_axis,
            "deterministic": True,
        },
    )


def _disease_lane_policy(
    question: str,
    entity: DiseaseEntity,
) -> tuple[dict[str, tuple[str, ...]], dict[str, Any], dict[str, str]]:
    lowered = question.casefold()
    statistics = any(
        marker in lowered for marker in ("환자수", "환자 수", "유병", "통계", "몇 명")
    )
    treatment = not statistics and any(
        marker in lowered
        for marker in ("치료", "약물", "파이프라인", "시장", "therapy", "treatment")
    )
    intent = "treatment" if treatment else "statistics"
    treatments = entity.treatments[:5] if treatment else ()
    updates: dict[str, tuple[str, ...]] = {
        "clinicaltrials": (f"{entity.condition} clinical trials",),
        "web": (f"{entity.canonical_name} 최신 근거",),
        "patent": (),
    }
    reasons = {"patent": "disease_only_without_brand_or_ingredient"}
    if treatments:
        updates["nedrug"] = tuple(f"{value} 성분 허가 정보" for value in treatments)
        updates["openfda"] = tuple(f"{value} active ingredient" for value in treatments)
    else:
        updates["nedrug"] = ()
        updates["openfda"] = ()
        reasons.update(
            {
                "nedrug": "statistics_intent_auxiliary_only",
                "openfda": "treatment_ingredient_not_requested",
            }
        )
    if entity.mart_axes:
        updates["mart"] = tuple(f"{axis} 일반 시장 데이터" for axis in entity.mart_axes)
    else:
        updates["mart"] = ()
        reasons["mart"] = "disease_market_mapping_unavailable"
    return (
        updates,
        {
            "status": "resolved",
            "intent": intent,
            "entity_id": entity.entity_id,
            "condition": entity.condition,
            "treatments": list(treatments),
            "expansion_grade": entity.expansion_grade,
            "source": entity.source,
            "fetched_at": entity.fetched_at,
            "clinical_axis": "condition",
            "ingredient_query_mode": "separate",
            "or_expansion": False,
        },
        reasons,
    )


def _mcp_disease_codes(
    question: str,
    interpreted: str,
    client: Any | None,
    *,
    prefer_cache: bool = True,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    from jw_chat_agent_poc.tool_use.integration import disease_query_from_question

    disease_query = disease_query_from_question(question) or disease_query_from_question(
        interpreted
    )
    if disease_query is None:
        return (), {"status": "not_applicable", "reason": "no_disease_query"}
    mapping = hira_disease_mapping(disease_query)
    if mapping is not None:
        selection = select_catalog_population(question, mapping.code_prefixes)
        codes = selection.codes
        return codes, {
            "status": "resolved_catalog",
            "query": disease_query,
            "catalog_basis": mapping.catalog_basis,
            "population_layer": selection.layer,
            "catalog_snapshot": {
                "fetched_at": selection.metadata.fetched_at,
                "total_count": selection.metadata.total_count,
                "page_count": selection.metadata.page_count,
                "response_sha256": list(selection.metadata.response_sha256),
            },
            "parent_codes": list(selection.resolution.parent_codes),
            "child_codes": list(selection.resolution.child_codes),
            "candidate_codes": list(codes),
            "rejected_codes": [],
            "selected_count": len(codes),
        }
    if client is None:
        catalog_parents = catalog_parent_codes_for_name(
            question,
        ) or catalog_parent_codes_for_name(disease_query)
        if catalog_parents:
            selection = select_catalog_population(question, catalog_parents)
            codes = selection.codes
            return codes, {
                "status": "resolved_catalog_name",
                "query": disease_query,
                "population_layer": selection.layer,
                "catalog_snapshot": {
                    "fetched_at": selection.metadata.fetched_at,
                    "total_count": selection.metadata.total_count,
                    "page_count": selection.metadata.page_count,
                    "response_sha256": list(selection.metadata.response_sha256),
                },
                "parent_codes": list(selection.resolution.parent_codes),
                "child_codes": list(selection.resolution.child_codes),
                "candidate_codes": list(codes),
                "rejected_codes": [],
                "selected_count": len(codes),
            }
        return (), {"status": "not_configured", "query": disease_query}
    if getattr(client, "mode", None) != "live":
        return (), {
            "status": "wrong_mode",
            "query": disease_query,
            "mode": str(getattr(client, "mode", "unknown")),
        }
    try:
        cached_call = getattr(client, "hira_disease_name_code_with_cache_status", None)
        direct_call = getattr(client, "hira_disease_name_code", None)
        if prefer_cache and callable(cached_call):
            call, cache_status = cached_call(disease_query)
        elif callable(direct_call):
            call = direct_call(disease_query)
            cache_status = "bypassed"
        elif callable(cached_call):
            call, _observed_cache_status = cached_call(disease_query)
            cache_status = "bypass_unavailable"
        else:
            raise AttributeError("HIRA disease-name lookup is unavailable")
    except Exception as exc:
        LOGGER.exception("HIRA disease-name expansion failed query=%r", disease_query)
        return (), {
            "status": "failed",
            "query": disease_query,
            "cache": "unknown",
            "error_type": type(exc).__name__,
        }
    call_status = str(getattr(call, "status", "unknown"))
    if call_status in {"error", "failed", "timeout", "deadline_exceeded"}:
        return (), {
            "status": "failed",
            "query": disease_query,
            "cache": cache_status,
            "call_status": call_status,
            "error_type": str(
                call.render_data.get("error_type")
                or ("timeout" if "timeout" in call_status else "provider_error")
            ),
        }
    items = call.render_data.get("items")
    code_text = (
        " ".join(
            str(item.get("sickCd") or item.get("sick_cd") or "")
            for item in items
            if isinstance(item, Mapping)
        )
        if isinstance(items, list)
        else ""
    )
    candidate_codes = _kcd_codes(code_text)
    filtered_codes = filter_hira_codes(disease_query, candidate_codes)
    query_catalog_parents = catalog_parent_codes_for_name(disease_query)
    fallback_catalog_parents = (
        () if query_catalog_parents else catalog_parent_codes_for_name(question)
    )
    selection = (
        select_catalog_population(
            question,
            query_catalog_parents or filtered_codes,
        )
        if filtered_codes
        else (
            select_catalog_population(question, fallback_catalog_parents)
            if fallback_catalog_parents
            else None
        )
    )
    codes = selection.codes if selection is not None else ()
    return codes, {
        "status": (
            "resolved_catalog_after_empty_lookup"
            if codes and not filtered_codes
            else ("resolved" if codes else "no_codes")
        ),
        "query": disease_query,
        "source_query": disease_query if codes and not filtered_codes else None,
        "cache": cache_status,
        "call_status": call_status,
        "population_layer": selection.layer if selection is not None else None,
        "catalog_snapshot": (
            {
                "fetched_at": selection.metadata.fetched_at,
                "total_count": selection.metadata.total_count,
                "page_count": selection.metadata.page_count,
                "response_sha256": list(selection.metadata.response_sha256),
            }
            if selection is not None
            else None
        ),
        "parent_codes": (
            list(selection.resolution.parent_codes) if selection is not None else []
        ),
        "child_codes": (
            list(selection.resolution.child_codes) if selection is not None else []
        ),
        "candidate_count": len(items) if isinstance(items, list) else 0,
        "candidate_codes": list(candidate_codes),
        "rejected_codes": [code for code in candidate_codes if code not in codes],
        "selected_count": len(codes),
    }


def build_second_hop_expansion(
    plan: PlannerOutput,
    question: str,
    results: Sequence[SourceResult],
    *,
    max_queries: int = 3,
) -> ExpansionOutcome | None:
    if max_queries <= 0:
        return None
    observed: list[tuple[str, str]] = []
    for result in results:
        if result.status != "ok":
            continue
        for value in _product_values(result.payload):
            if value.casefold() in question.casefold():
                continue
            pair = (value, result.source)
            if pair not in observed:
                observed.append(pair)
    if not observed:
        return None
    selected = observed[:max_queries]
    updates = {
        source: tuple(
            f"{entity} {_QUERY_SUFFIX[source]}"
            for entity, _origin in selected
        )
        for source in tool_query_sources(plan.answer_sources)
    }
    expanded = plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(update=updates),
            "needs_second_hop": False,
        }
    )
    return ExpansionOutcome(
        plan=expanded,
        trace={
            "status": "expanded",
            "source": selected[0][1],
            "entities": [entity for entity, _origin in selected],
            "requests": {
                source: list(queries) for source, queries in sorted(updates.items())
            },
            "candidate_count": len(observed),
            "truncated_count": max(0, len(observed) - len(selected)),
            "max_queries": max_queries,
            "deterministic": True,
        },
    )


def _expand_disease_molecules(
    plan: PlannerOutput,
    question: str,
    *,
    codes: Sequence[str],
    seed_brands: Sequence[str],
    molecule_reader: Any | None,
) -> dict[str, Any] | None:
    candidates = _normalized_exact_unique(
        ingredient.strip()
        for concept in plan.clinical_query_specs
        for ingredient in concept.ingredients
        if _is_molecule_candidate(ingredient, question)
    )
    if not (candidates or seed_brands) or not codes:
        return None
    if molecule_reader is None:
        from jw_chat_agent_poc.agent_loop.factory import default_brand_molecule_reader

        molecule_reader = default_brand_molecule_reader()
    rows = brand_molecule_records(molecule_reader)
    candidate_keys = {_exact_key(candidate): candidate for candidate in candidates}
    brand_keys = {_exact_key(brand) for brand in seed_brands}
    matched_rows = tuple(
        row
        for row in rows
        if _exact_key(str(row.get("molecule_norm") or "")) in candidate_keys
        or _exact_key(str(row.get("molecule_display") or "")) in candidate_keys
        or _exact_key(str(row.get("brand_name") or "")) in brand_keys
    )
    matched_candidate_keys = {
        _exact_key(str(value))
        for row in matched_rows
        for value in (row.get("molecule_norm"), row.get("molecule_display"))
        if value
    }
    validated = _normalized_exact_unique(
        str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
        for row in matched_rows
        if str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
    )
    brands = _normalized_exact_unique(
        (
            *seed_brands,
            *(
                str(row.get("brand_name") or "").strip()
                for row in matched_rows
                if str(row.get("brand_name") or "").strip()
            ),
        )
    )[: configured_entity_limit()]
    atc4_codes = tuple(
        dict.fromkeys(
            str(row.get("atc4_code") or "").strip().upper()
            for row in matched_rows
            if str(row.get("atc4_code") or "").strip()
        )
    )[: configured_entity_limit()]
    all_mart_queries = _normalized_exact_unique(
        (
            *(f"{brand} 내부 시장 데이터" for brand in brands),
            *(f"{molecule} 성분 시장 데이터" for molecule in validated),
            *(f"{atc4} 일반 시장 데이터" for atc4 in atc4_codes),
        )
    )
    mart_queries = all_mart_queries[:DISEASE_MART_FANOUT_LIMIT]
    omitted_mart_queries = all_mart_queries[DISEASE_MART_FANOUT_LIMIT:]
    external_candidates = _normalized_exact_unique((*validated, *candidates))[
        : configured_entity_limit()
    ]
    requests: dict[str, tuple[str, ...]] = {
        "nedrug": tuple(f"{value} 성분 허가 정보" for value in external_candidates),
        "openfda": tuple(f"{value} active ingredient" for value in external_candidates),
        "patent": tuple(
            dict.fromkeys(
                (
                    *(f"{value} 특허현황" for value in external_candidates),
                    *(f"{brand} 특허현황" for brand in brands),
                )
            )
        ),
        "web": tuple(f"{question} {value}" for value in external_candidates),
    }
    if mart_queries:
        requests["mart"] = mart_queries
    return {
        "status": "expanded",
        "source": "planner_candidate_plus_mart_brand_molecule",
        "display_scope": "query_only",
        "original_entities": list(plan.requested_answer_shape.entities),
        "candidates": list(candidates),
        "validated_molecules": list(validated),
        "brands": list(brands),
        "atc4_codes": list(atc4_codes),
        "mart_fanout_stage": "mart_dictionary_validation",
        "mart_fanout_limit": DISEASE_MART_FANOUT_LIMIT,
        "mart_fanout_requested": len(all_mart_queries),
        "mart_fanout_executed": len(mart_queries),
        "omitted_requests": {"mart": list(omitted_mart_queries)},
        "unvalidated_candidates": [
            candidate
            for candidate in candidates
            if _exact_key(candidate) not in matched_candidate_keys
        ],
        "requests": requests,
        "deterministic_validation": "normalized_exact_match",
    }


def _exact_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _normalized_exact_unique(values: Iterable[str]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for value in values:
        normalized = str(value).strip()
        if normalized:
            unique.setdefault(_exact_key(normalized), normalized)
    return tuple(unique.values())


def _is_molecule_candidate(value: str, question: str) -> bool:
    normalized = " ".join(value.split())
    if not normalized or _exact_key(normalized) == _exact_key(question):
        return False
    lowered = normalized.casefold()
    return not any(token in lowered for token in ("알려줘", "보여줘", "시장 현황"))


def _kcd_codes(question: str) -> tuple[str, ...]:
    match = _KCD_RANGE_RE.search(question.upper())
    if match is not None:
        start = int(match.group("start"))
        end = int(match.group("end"))
        if end < start or end - start > 20:
            return ()
        prefix = match.group("prefix").upper()
        return tuple(f"{prefix}{value:02d}" for value in range(start, end + 1))
    return tuple(
        dict.fromkeys(
            match.group("code").upper().replace(".", "").replace("_", "")
            for match in _KCD_SINGLE_RE.finditer(question.upper())
        )
    )


def _years(question: str, observed_on: date) -> tuple[int, ...]:
    year_range = _YEAR_RANGE_RE.search(question)
    if year_range is not None:
        start = _calendar_year(year_range.group("start"))
        end = _calendar_year(year_range.group("end"))
        if start <= end and end - start <= 20:
            return tuple(range(start, end + 1))
        return ()
    explicit = tuple(
        dict.fromkeys(_calendar_year(value) for value in _YEAR_TOKEN_RE.findall(question))
    )
    if explicit:
        return explicit
    recent = _RECENT_YEARS_RE.search(question)
    if recent is None:
        return ()
    count = min(max(int(recent.group("count")), 1), 10)
    return tuple(range(observed_on.year - count + 1, observed_on.year + 1))


def _query_subject(
    question: str,
    codes: Sequence[str],
    years: Sequence[int],
) -> str:
    value = _KCD_RANGE_RE.sub(" ", question.upper(), count=1)
    value = _KCD_SINGLE_RE.sub(" ", value)
    for code in codes:
        value = value.replace(code, " ")
    value = _YEAR_RANGE_RE.sub(" ", value)
    value = _YEAR_TOKEN_RE.sub(" ", value)
    value = re.sub(r"(?:상병\s*코드|년도별|연도별)", " ", value)
    value = re.sub(r"(?:^|\s)(?:과|와|및)(?=\s|$)", " ", value)
    value = re.sub(r"\b(?:비교|알려줘|보여줘)\b", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split()).strip()


def _strip_years(value: str) -> str:
    without_range = _YEAR_RANGE_RE.sub(" ", value)
    return " ".join(_YEAR_TOKEN_RE.sub(" ", without_range).split())


def _calendar_year(value: str) -> int:
    year = int(value)
    return year if year >= 1000 else 2000 + year


def _product_values(value: Any) -> tuple[str, ...]:
    output: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key).casefold() in _PRODUCT_KEYS and isinstance(nested, str):
                    cleaned = " ".join(nested.split())
                    if 1 < len(cleaned) <= 80 and cleaned not in output:
                        output.append(cleaned)
                else:
                    visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(output)
