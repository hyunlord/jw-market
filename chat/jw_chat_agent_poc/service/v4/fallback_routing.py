from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.hira_catalog import (
    catalog_parent_codes_for_name,
    select_catalog_population,
)
from jw_chat_agent_poc.hira_surface import mentions_hira_axis, requested_hira_axes
from jw_chat_agent_poc.resolver.brand_resolver import UnsupportedBrandError
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    deterministic_clinical_class_requests,
    single_base_mart_ingredient,
)
from jw_chat_agent_poc.service.v4.contracts import ClinicalTrialConcept, PlannerOutput
from jw_chat_agent_poc.service.v4.entity_registry import resolve_disease_entity
from jw_chat_agent_poc.tool_use.integration import disease_query_from_question
from jw_chat_agent_poc.tool_use.routing_v4_rules import explicit_disease_code


@dataclass(frozen=True, slots=True)
class FallbackRoutingOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]
    notice: str | None


@dataclass(frozen=True, slots=True)
class ExplicitSubstanceOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AllSourceQueryOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]


_EXPLICIT_SUBSTANCE_RE = re.compile(
    r"^(?P<term>.+?)\s*(?:으로|로|을|를)?\s*"
    r"(?P<source>클리니컬(?:트라이얼스?|스)?|clinical\s*trials?|"
    r"임상(?:시험|현황)?|open\s*fda|fda)(?:에서|로)?(?P<tail>.*)$",
    re.IGNORECASE,
)
_EXPLICIT_CLASS_SCOPE_RE = re.compile(
    r"(?:\s*(?:계열(?:\s*전체)?|구성\s*성분|성분별|성분\s*각각|파생\s*제품))\s*$",
    re.IGNORECASE,
)

_DISEASE_QUERY_CONTRACTS: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("이상지질혈증", "고지혈증"), "E78", ("C10A1", "C10C")),
)



def compose_all_source_queries(
    plan: PlannerOutput,
    question: str,
    *,
    resolver: Any | None = None,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None = None,
) -> AllSourceQueryOutcome:
    """Compose lane-owned queries without copying the user sentence across tools."""

    normalized_question = " ".join(question.split())
    disease = explicit_disease_code(question) or disease_query_from_question(question)
    disease_label, disease_code, disease_atcs = _disease_query_contract(
        normalized_question,
        disease,
    )
    explicit_atcs = tuple(
        dict.fromkeys(
            match.group(1).upper()
            for match in re.finditer(
                r"\bATC\s*3\s*[:=]?\s*([A-Z]\d{2}[A-Z])\b",
                normalized_question,
                re.IGNORECASE,
            )
        )
    )
    resolutions = _resolved_query_brands(resolver, normalized_question)
    if not resolutions and resolver is not None and re.search(
        r"(?:JW|제이더블유|중외)", normalized_question, re.IGNORECASE
    ):
        resolutions = _matching_portfolio_brands(
            resolver,
            disease_label=disease_label,
        )

    brands = tuple(
        dict.fromkeys(
            str(getattr(item, "canonical_brand", "")).strip()
            for item in resolutions
            if str(getattr(item, "canonical_brand", "")).strip()
        )
    )
    molecules = tuple(
        dict.fromkeys(
            str(value).strip()
            for item in resolutions
            for value in getattr(item, "molecule_en", ())
            if str(value).strip() and not re.search(r"[가-힣]", str(value))
        )
    )
    atcs = tuple(
        dict.fromkeys(
            (
                *disease_atcs,
                *explicit_atcs,
                *(
                    str(value).strip()
                    for item in resolutions
                    for value in getattr(item, "atc", ())
                    if str(value).strip()
                ),
            )
        )
    )
    canonical_source: str | None = None
    if len(brands) == 1 and molecule_fallback is not None:
        try:
            values, status = molecule_fallback(brands[0])
        except (OSError, TimeoutError):
            values, status = (), "failed"
        canonical_molecules = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in values
                if str(value).strip() and not re.search(r"[가-힣]", str(value))
            )
        )
        if status == "resolved" and canonical_molecules and (
            not molecules or len(canonical_molecules) == 1
        ):
            base_ingredient = single_base_mart_ingredient(
                molecules,
                canonical_molecules,
            )
            molecules = (base_ingredient,) if base_ingredient else canonical_molecules
            canonical_source = (
                "mart_brand_molecule+nedrug_ingredient"
                if base_ingredient
                else "nedrug_ingredient"
            )
    elif brands and not molecules and molecule_fallback is not None:
        resolved: list[str] = []
        for brand in brands[:2]:
            try:
                values, _status = molecule_fallback(brand)
            except (OSError, TimeoutError):
                continue
            resolved.extend(
                str(value).strip()
                for value in values
                if str(value).strip() and not re.search(r"[가-힣]", str(value))
            )
        molecules = tuple(dict.fromkeys(resolved))

    brand_ingredient_mappings = tuple(
        {
            "brand": str(getattr(item, "canonical_brand", "")).strip(),
            "source": str(getattr(item, "support_source", "") or "unknown"),
            "ingredients": tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in getattr(item, "molecule_en", ())
                    if str(value).strip() and not re.search(r"[가-힣]", str(value))
                )
            ),
        }
        for item in resolutions
        if str(getattr(item, "canonical_brand", "")).strip()
    )
    if len(brands) == 1 and molecules and brand_ingredient_mappings:
        mapping_source = canonical_source or str(
            brand_ingredient_mappings[0].get("source") or "nedrug_ingredient"
        )
        brand_ingredient_mappings = (
            {
                "brand": brands[0],
                "source": mapping_source,
                "ingredients": molecules,
            },
        )

    web_query, web_query_trace = _web_query_contract(
        brands=brands,
        molecules=molecules,
        disease_label=disease_label,
        disease_code=disease_code,
        atcs=atcs,
    )
    composed = _lane_query_map(
        brands=brands,
        molecules=molecules,
        disease_label=disease_label,
        disease_code=disease_code,
        atcs=atcs,
        web_query=web_query,
    )
    query_updates: dict[str, tuple[str, ...]] = {}
    unavailable: dict[str, str] = {}
    hira_catalog_queries_preserved = 0
    hira_catalog_queries_available = 0
    hira_catalog_queries_omitted = 0
    hira_catalog_population_layer: str | None = None
    hira_catalog_source_query_count = 0
    hira_requested_axes: tuple[str, ...] = ()
    hira_axis_auto_expanded = False
    raw_casefold = normalized_question.casefold()
    discarded_raw_queries: dict[str, tuple[str, ...]] = {}
    discarded_brand_queries: dict[str, tuple[str, ...]] = {}
    for source, generated in composed.items():
        discarded = tuple(
            normalized
            for value in getattr(plan.tool_queries, source)
            if (normalized := " ".join(str(value).split()))
            and raw_casefold in normalized.casefold()
        )
        if discarded:
            discarded_raw_queries[source] = discarded
        existing_values: list[str] = []
        discarded_brand_values: list[str] = []
        for value in getattr(plan.tool_queries, source):
            normalized = " ".join(str(value).split())
            if not normalized or raw_casefold in normalized.casefold():
                continue
            if source in {"clinicaltrials", "openfda", "web"} and brands and (
                not molecules
                or not any(
                    molecule.casefold() in normalized.casefold()
                    for molecule in molecules
                )
            ):
                discarded_brand_values.append(normalized)
                continue
            existing_values.append(normalized)
        existing = tuple(dict.fromkeys(existing_values))
        if discarded_brand_values:
            discarded_brand_queries[source] = tuple(
                dict.fromkeys(discarded_brand_values)
            )
        catalog_hira_queries = (
            tuple(
                query
                for query in existing
                if re.search(r"(?<![A-Z0-9])[A-Z]\d{3,4}(?![A-Z0-9])", query)
            )
            if source == "hira"
            else ()
        )
        catalog_parents = (
            (
                catalog_parent_codes_for_name(disease_label or "")
                or catalog_parent_codes_for_name(normalized_question)
            )
            if source == "hira"
            else ()
        )
        if catalog_parents:
            population = select_catalog_population(question, catalog_parents)
            existing_by_code = {
                match.group(0): query
                for query in catalog_hira_queries
                if (
                    match := re.search(
                        r"(?<![A-Z0-9])[A-Z]\d{2,4}(?![A-Z0-9])",
                        query,
                    )
                )
            }
            hira_requested_axes = requested_hira_axes(normalized_question)
            axis_suffix = (
                " 성별·연령별"
                if any(axis in {"sex", "age"} for axis in hira_requested_axes)
                else ""
            )
            disease_query = disease_label or normalized_question
            if population.layer == "subcode":
                catalog_queries = tuple(
                    existing_by_code.get(
                        code,
                        f"{code} {disease_query} 환자수{axis_suffix}",
                    )
                    for code in population.codes
                )
                selected = catalog_queries
            else:
                catalog_queries = tuple(
                    f"{code} {disease_query} 환자수{axis_suffix}"
                    for code in population.codes
                )
                selected = catalog_queries
            hira_catalog_population_layer = population.layer
            hira_catalog_queries_available = len(catalog_queries)
            hira_catalog_source_query_count = len(selected)
            hira_catalog_queries_preserved = len(selected)
            hira_catalog_queries_omitted = len(catalog_queries) - len(selected)
        else:
            selected = tuple(dict.fromkeys((*generated, *existing)))[:2]
        query_updates[source] = selected
        if not selected:
            unavailable[source] = (
                "ingredient_unresolved"
                if source in {"clinicaltrials", "openfda"} and brands and not molecules
                else "query_construction_unavailable"
            )

    resolved_entities = tuple(
        dict.fromkeys(
            (
                *plan.answer_contract.resolved_entities,
                *brands,
                *molecules,
            )
        )
    )
    answer_contract = plan.answer_contract
    unrequested_axis_items_removed = 0
    if hira_catalog_population_layer and not hira_requested_axes:
        retained_items = tuple(
            item
            for item in answer_contract.required_items
            if not mentions_hira_axis(f"{item.id} {item.ask}")
        )
        unrequested_axis_items_removed = (
            len(answer_contract.required_items) - len(retained_items)
        )
        retained_dimensions = tuple(
            dimension
            for dimension in answer_contract.required_dimensions
            if not mentions_hira_axis(dimension)
        )
        answer_contract = answer_contract.model_copy(
            update={
                "required_items": retained_items,
                "required_dimensions": retained_dimensions,
            }
        )

    return AllSourceQueryOutcome(
        plan=plan.model_copy(
            update={
                "tool_queries": plan.tool_queries.model_copy(update=query_updates),
                "answer_contract": answer_contract.model_copy(
                    update={"resolved_entities": resolved_entities}
                ),
            }
        ),
        trace={
            "brands": brands,
            "molecules": molecules,
            "disease": disease_label,
            "disease_code": disease_code,
            "atc": atcs,
            "query_construction_unavailable": unavailable,
            "raw_question_fallback_count": 0,
            "discarded_raw_question_queries": discarded_raw_queries,
            "discarded_foreign_brand_queries": discarded_brand_queries,
            "brand_ingredient_mappings": brand_ingredient_mappings,
            "hira_catalog_population_layer": hira_catalog_population_layer,
            "hira_catalog_queries_available": hira_catalog_queries_available,
            "hira_catalog_queries_preserved": hira_catalog_queries_preserved,
            "hira_catalog_queries_omitted": hira_catalog_queries_omitted,
            "hira_catalog_source_query_count": hira_catalog_source_query_count,
            "hira_requested_axes": hira_requested_axes,
            "hira_axis_auto_expanded": hira_axis_auto_expanded,
            "hira_unrequested_axis_items_removed": unrequested_axis_items_removed,
            "web_query_transformations": [web_query_trace] if web_query_trace else [],
        },
    )


def _resolved_query_brands(resolver: Any | None, question: str) -> tuple[Any, ...]:
    if resolver is None:
        return ()
    try:
        return tuple(resolver.resolve_many(question, allow_default=False))
    except (UnsupportedBrandError, LookupError, OSError, TimeoutError):
        return ()


def _matching_portfolio_brands(
    resolver: Any,
    *,
    disease_label: str | None,
) -> tuple[Any, ...]:
    try:
        portfolio = tuple(resolver.portfolio_brands())
    except (AttributeError, LookupError, OSError, TimeoutError):
        return ()
    if not disease_label:
        return portfolio
    aliases = {disease_label.replace("이상지질혈증", "고지혈증")}
    aliases.add(disease_label.replace("고지혈증", "이상지질혈증"))
    matched = tuple(
        item
        for item in portfolio
        if any(
            alias and alias in str(name)
            for alias in aliases
            for name in (
                getattr(item, "market_name", None),
                *getattr(item, "market_names", ()),
            )
            if name
        )
    )
    return matched or portfolio


def _disease_query_contract(
    question: str,
    detected: str | None,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    for aliases, code, atcs in _DISEASE_QUERY_CONTRACTS:
        if any(alias in question for alias in aliases):
            label = next(alias for alias in aliases if alias in question)
            return label, code, atcs
    return detected, explicit_disease_code(question), ()


def _lane_query_map(
    *,
    brands: tuple[str, ...],
    molecules: tuple[str, ...],
    disease_label: str | None,
    disease_code: str | None,
    atcs: tuple[str, ...],
    web_query: str = "",
) -> dict[str, tuple[str, ...]]:
    brand_text = " ".join(brands[:4])
    molecule_text = " AND ".join(molecules[:2])
    disease_text = disease_label or disease_code or ""
    market_terms = " ".join(value for value in (*atcs, brand_text) if value)
    subject = brand_text or disease_text or (atcs[0] if atcs else "")
    foreign_subject = molecule_text or (disease_text if not brands else "")
    return {
        "mart": ((f"{disease_text} {market_terms} 내부 시장 매출 경쟁".strip(),) if market_terms else ()),
        "nedrug": tuple(f"{value} 성분 허가 정보" for value in molecules[:2])
        or tuple(f"{value} 의약품 허가 정보" for value in brands[:2])
        or ((f"{disease_text or subject} 의약품 허가 정보",) if disease_text or subject else ()),
        "hira": ((f"{disease_code or disease_text} 환자 통계",) if disease_text else ((f"{subject} 관련 급여 환자 통계",) if subject else ())),
        "openfda": tuple(f"{value} label safety" for value in molecules[:2])
        or ((f"{foreign_subject} category safety",) if foreign_subject else ()),
        "clinicaltrials": ((f"{molecule_text} clinical trials",) if molecule_text else ((f"{foreign_subject} clinical trials",) if foreign_subject else ())),
        "web": ((web_query,) if web_query else ()),
        "patent": tuple(f"{value} 특허현황" for value in brands[:2])
        or tuple(f"{value} 특허현황" for value in molecules[:2])
        or ((f"{disease_text or subject} 관련 특허현황",) if disease_text or subject else ()),
    }


def _web_query_contract(
    *,
    brands: tuple[str, ...],
    molecules: tuple[str, ...],
    disease_label: str | None,
    disease_code: str | None,
    atcs: tuple[str, ...],
) -> tuple[str, dict[str, Any] | None]:
    brand_text = " ".join(brands[:4])
    molecule_text = " AND ".join(molecules[:2])
    disease_text = disease_label or disease_code or ""
    subject = brand_text or disease_text or (atcs[0] if atcs else "")
    foreign_subject = molecule_text or (disease_text if not brands else "")
    web_subject = foreign_subject if brands and molecules else subject
    before = " ".join(
        value for value in (web_subject, disease_text, "공식 최신 정보") if value
    )
    if molecules:
        identifier_type = "molecule"
        qualified_subject = f"{molecule_text} 의약품"
    elif brands:
        identifier_type = "brand"
        qualified_subject = f"{brand_text} 의약품"
    elif disease_label or disease_code:
        identifier_type = "disease"
        disease_entity = resolve_disease_entity(disease_text)
        readable_disease = (
            disease_entity.canonical_name if disease_entity is not None else disease_text
        )
        qualified_subject = f"{readable_disease} 질환"
    elif atcs:
        identifier_type = "atc3"
        qualified_subject = f"ATC3 의약품 분류 {' '.join(atcs)}"
    else:
        return "", None
    after = f"{qualified_subject} 공식 최신 정보"
    return after, {
        "identifier_type": identifier_type,
        "before": before,
        "after": after,
        "changed": before != after,
    }


def augment_explicit_substance_queries(
    plan: PlannerOutput,
    question: str,
    *,
    resolver: Any | None = None,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None = None,
) -> ExplicitSubstanceOutcome:
    """Bind a user-named substance exactly across foreign-source lanes."""

    explicit_term = _explicit_substance_term(question)
    if explicit_term is None:
        return ExplicitSubstanceOutcome(
            plan=plan,
            trace={"applied": False, "reason": "no_explicit_substance"},
        )
    class_match = _EXPLICIT_CLASS_SCOPE_RE.search(explicit_term)
    if class_match is not None:
        class_anchor = explicit_term
        while _EXPLICIT_CLASS_SCOPE_RE.search(class_anchor):
            class_anchor = _EXPLICIT_CLASS_SCOPE_RE.sub("", class_anchor).strip()
        if not class_anchor:
            return ExplicitSubstanceOutcome(
                plan=plan,
                trace={"applied": False, "reason": "class_anchor_unresolved"},
            )
        class_requests = deterministic_clinical_class_requests(question)
        class_queries = tuple(query for query, _concept in class_requests)
        class_concepts = tuple(concept for _query, concept in class_requests)
        if not class_queries:
            class_queries = (class_anchor,)
            class_concepts = (
                ClinicalTrialConcept(
                    ingredients=(class_anchor,),
                    search_area="intervention",
                    match="any",
                    source_queries=(class_anchor,),
                    expansion_source="entity_variant_dictionary",
                    expansion_status="failed",
                    expansion_grade="notation_variant",
                ),
            )
        updated = plan.model_copy(
            update={
                "tool_queries": plan.tool_queries.model_copy(
                    update={
                        "clinicaltrials": class_queries,
                        "openfda": class_queries,
                        "web": class_queries,
                        "nedrug": class_queries,
                        "patent": class_queries,
                    }
                ),
                "clinical_query_specs": class_concepts,
                "answer_contract": plan.answer_contract.model_copy(
                    update={
                        "resolved_entities": tuple(
                            dict.fromkeys(
                                (*plan.answer_contract.resolved_entities, class_anchor)
                            )
                        )
                    }
                ),
            }
        )
        return ExplicitSubstanceOutcome(
            plan=updated,
            trace={
                "applied": True,
                "explicit_term": explicit_term,
                "queries": class_queries,
                "translation_attempted": False,
                "translation_status": (
                    "resolved" if class_requests else "class_dictionary_unresolved"
                ),
                "expansion_grade": "explicit_class",
                "replaced_planner_queries": True,
            },
        )
    if "제네릭" in explicit_term:
        return ExplicitSubstanceOutcome(
            plan=plan,
            trace={"applied": False, "reason": "brand_scope_owned_by_resolver"},
        )
    brand_resolutions = _resolved_query_brands(resolver, explicit_term)
    if any(
        str(getattr(item, "canonical_brand", "")).strip()
        for item in brand_resolutions
    ):
        return ExplicitSubstanceOutcome(
            plan=plan,
            trace={"applied": False, "reason": "brand_scope_owned_by_resolver"},
        )

    translation_attempted = bool(re.search(r"[가-힣]", explicit_term))
    translation_status = "not_needed"
    resolved_terms: tuple[str, ...] = (explicit_term,)
    if translation_attempted:
        translated: tuple[str, ...] = ()
        status = "failed"
        if molecule_fallback is not None:
            try:
                values, status = molecule_fallback(explicit_term)
                translated = tuple(
                    dict.fromkeys(
                        " ".join(str(value).split())
                        for value in values
                        if str(value).strip() and not re.search(r"[가-힣]", str(value))
                    )
                )
            except (OSError, TimeoutError):
                status = "failed"
        if translated:
            resolved_terms = translated
            translation_status = "resolved"
        else:
            resolved_terms = ()
            translation_status = f"{status}_ingredient_unresolved"

    # An explicit designation is an exclusive scope, not another expansion term.
    # Keep the user's spelling when foreign ingredient resolution is unavailable so
    # the lane reports an unresolved/empty result honestly without broadening it.
    exact_terms = resolved_terms or (explicit_term,)
    clinical_queries = exact_terms
    openfda_queries = exact_terms
    web_queries = exact_terms
    new_concepts = tuple(
        ClinicalTrialConcept(
            ingredients=(term,),
            search_area="intervention",
            match="any",
            source_queries=(term,),
            expansion_source=("nedrug_ingredient" if translation_attempted else "none"),
            expansion_status=(
                "resolved"
                if translation_status == "resolved"
                else "failed"
                if translation_attempted
                else "not_requested"
            ),
            expansion_grade="notation_variant",
        )
        for term in exact_terms
    )
    updated = plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(
                update={
                    "clinicaltrials": clinical_queries,
                    "openfda": openfda_queries,
                    "web": web_queries,
                    "nedrug": exact_terms,
                    "patent": exact_terms,
                }
            ),
            "clinical_query_specs": new_concepts,
            "answer_contract": plan.answer_contract.model_copy(
                update={
                    "resolved_entities": tuple(
                        dict.fromkeys(
                            (
                                *plan.answer_contract.resolved_entities,
                                explicit_term,
                                *resolved_terms,
                            )
                        )
                    )
                }
            ),
        }
    )
    return ExplicitSubstanceOutcome(
        plan=updated,
        trace={
            "applied": True,
            "explicit_term": explicit_term,
            "queries": exact_terms,
            "translation_attempted": translation_attempted,
            "translation_status": translation_status,
            "expansion_grade": "exact_designation",
            "replaced_planner_queries": True,
        },
    )


def _explicit_substance_term(question: str) -> str | None:
    normalized = " ".join(question.split()).strip()
    match = _EXPLICIT_SUBSTANCE_RE.search(normalized)
    if match is None:
        return None
    term = re.sub(r"(?:으로|로|을|를)$", "", match.group("term").strip())
    term = term.strip(" ,:;·?!.。？！")
    if not term or len(term) > 80 or not re.search(r"[가-힣A-Za-z0-9]", term):
        return None
    return term


def _append_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    values: dict[str, str] = {
        " ".join(value.split()).casefold(): value for value in existing if value.strip()
    }
    for value in additions:
        normalized = " ".join(value.split())
        values.setdefault(normalized.casefold(), normalized)
    return tuple(values.values())


def _prepend_unique(existing: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    return _append_unique(additions, existing)


def sanitize_planner_fallback(
    plan: PlannerOutput,
    question: str,
    planner_trace: Mapping[str, Any],
    *,
    resolver: Any | None = None,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None = None,
) -> FallbackRoutingOutcome:
    if str(planner_trace.get("status") or "") != "fallback":
        return FallbackRoutingOutcome(
            plan=plan,
            trace={"applied": False, "reason": "planner_not_fallback"},
            notice=None,
        )
    if not _is_patient_count_intent(question):
        brand_outcome = _brand_fallback(plan, question, resolver, molecule_fallback)
        if brand_outcome is not None:
            return brand_outcome
        return FallbackRoutingOutcome(
            plan=plan,
            trace={"applied": False, "reason": "not_patient_count_intent"},
            notice=None,
        )

    explicit_code = explicit_disease_code(question)
    disease_or_code = explicit_code or disease_query_from_question(question)
    mixed_queries = _mixed_patient_market_queries(question)
    if mixed_queries is not None:
        patient_query, market_query = mixed_queries
        disease_label = disease_or_code or patient_query
        queries = plan.tool_queries.model_copy(
            update={
                "mart": (market_query,),
                "nedrug": (f"{disease_label} 의약품 허가 정보",),
                "hira": (patient_query,),
                "openfda": (f"{disease_label} disease safety",),
                "clinicaltrials": (f"{disease_label} clinical trials",),
                "web": (f"{disease_label} 공식 통계",),
                "patent": (f"{disease_label} 특허현황",),
            }
        )
        market_source = "document" if "document" in plan.answer_sources else "mart"
        return FallbackRoutingOutcome(
            plan=plan.model_copy(
                update={
                    "answer_sources": ("hira", market_source),
                    "tool_queries": queries,
                }
            ),
            trace={
                "applied": True,
                "intent": "mixed_patient_market",
                "disease_query": disease_or_code,
                "patient_query": patient_query,
                "market_query": market_query,
                "executed_sources": tuple(
                    source for source, values in queries.items() if values
                ),
                "all_tool_lanes_planned": all(
                    bool(values) for _source, values in queries.items()
                ),
                "omitted_reasons": {},
            },
            notice=(
                "질문 해석이 완료되지 않아 결정론적으로 환자 통계와 시장 총액 축을 "
                "분리해 조회했습니다."
            ),
        )

    hira_queries = (
        (_patient_query(question, disease_or_code),)
        if disease_or_code is not None
        else ()
    )
    if disease_or_code is None:
        unresolved_queries = plan.tool_queries.model_copy(
            update={
                "mart": (),
                "nedrug": (),
                "hira": (),
                "openfda": (),
                "clinicaltrials": (),
                "web": (question,),
                "patent": (),
            }
        )
        return FallbackRoutingOutcome(
            plan=plan.model_copy(update={"tool_queries": unresolved_queries}),
            trace={
                "applied": False,
                "intent": "patient_count",
                "disease_query": None,
                "all_tool_lanes_planned": all(
                    bool(values) for _source, values in unresolved_queries.items()
                ),
                "omitted_reasons": {"hira": "missing_disease_or_kcd"},
            },
            notice="질문 해석이 완료되지 않아 상병코드 또는 질환명을 확정하지 못했습니다.",
        )
    queries = plan.tool_queries.model_copy(
        update={
            "mart": (f"{disease_or_code} 내부 시장 데이터",),
            "nedrug": (f"{disease_or_code} 의약품 허가 정보",),
            "hira": hira_queries,
            "openfda": (f"{disease_or_code} disease safety",),
            "clinicaltrials": (f"{disease_or_code} clinical trials",),
            "web": (f"{disease_or_code} 공식 통계",),
            "patent": (f"{disease_or_code} 특허현황",),
        }
    )
    return FallbackRoutingOutcome(
        plan=plan.model_copy(update={"tool_queries": queries}),
        trace={
            "applied": True,
            "intent": "patient_count",
            "disease_query": disease_or_code,
            "executed_sources": tuple(
                source for source, values in queries.items() if values
            ),
            "all_tool_lanes_planned": all(
                bool(values) for _source, values in queries.items()
            ),
            "omitted_reasons": {},
        },
        notice=(
            f"질문 해석이 완료되지 않아 {disease_or_code} 질환명·코드 기반 "
            "결정론 질의로 전 자료원을 조회했습니다."
        ),
    )


def _brand_fallback(
    plan: PlannerOutput,
    question: str,
    resolver: Any | None,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None,
) -> FallbackRoutingOutcome | None:
    if resolver is None:
        return None
    try:
        resolutions = tuple(resolver.resolve_many(question, allow_default=False))
    except (UnsupportedBrandError, LookupError, OSError, TimeoutError):
        return None
    if not resolutions:
        return None

    resolution = resolutions[0]
    brand = str(resolution.canonical_brand).strip()
    molecules = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in resolution.molecule_en
            if str(value).strip()
        )
    )
    molecule_source = (
        "mart_molecule"
        if molecules and "mart" in str(resolution.support_source).casefold()
        else "none"
    )
    if not molecules and molecule_fallback is not None:
        try:
            fallback_molecules, status = molecule_fallback(brand)
        except (OSError, TimeoutError):
            fallback_molecules, status = (), "failed"
        molecules = tuple(
            dict.fromkeys(value.strip() for value in fallback_molecules if value.strip())
        )
        molecule_source = "nedrug_ingredient" if molecules else f"nedrug_{status}"

    primary = molecules[0] if molecules else brand
    ingredient_queries = tuple(
        f"{molecule} 성분 허가 정보" for molecule in molecules[:2]
    )
    clinical_queries = (f"{brand} 임상 현황",) + tuple(molecules[:1])
    patent_queries = (f"{brand} 특허현황",) + tuple(
        f"{molecule} 특허현황" for molecule in molecules[:1]
    )
    queries = plan.tool_queries.model_copy(
        update={
            "mart": (f"{brand} 내부 시장 데이터",),
            "nedrug": ingredient_queries or (f"{brand} 의약품 허가 정보",),
            "hira": (f"{brand} 급여 환자 통계",),
            "openfda": (f"{primary} label safety",),
            "clinicaltrials": clinical_queries[:2],
            "web": (f"{brand} 공식 최신 정보",),
            "patent": patent_queries[:2],
        }
    )
    return FallbackRoutingOutcome(
        plan=plan.model_copy(update={"tool_queries": queries}),
        trace={
            "applied": True,
            "intent": "brand",
            "brand": brand,
            "molecules": molecules,
            "molecule_source": molecule_source,
            "executed_sources": tuple(
                source for source, values in queries.items() if values
            ),
            "all_tool_lanes_planned": all(
                bool(values) for _source, values in queries.items()
            ),
            "omitted_reasons": {},
        },
        notice=(
            f"질문 해석이 완료되지 않아 {brand}의 결정론 브랜드·성분 질의로 "
            "전 자료원을 조회했습니다."
        ),
    )


def _patient_query(question: str, disease_or_code: str) -> str:
    query = _strip_request_suffix(question)
    return query if query else f"{disease_or_code} 환자수"


def _strip_request_suffix(question: str) -> str:
    normalized = " ".join(question.split())
    return re.sub(
        r"\s*(?:알려\s*(?:줘|주세요)|비교해\s*(?:줘|주세요)|보여\s*(?:줘|주세요))"
        r"\s*[?.!。？！]*\s*$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()


def _mixed_patient_market_queries(question: str) -> tuple[str, str] | None:
    normalized = _strip_request_suffix(question)
    parts = tuple(
        part.strip(" ,")
        for part in re.split(r"(?:와|과)\s+|\s+및\s+", normalized)
        if part.strip(" ,")
    )
    patient = tuple(part for part in parts if _is_patient_count_intent(part))
    market = tuple(
        part
        for part in parts
        if re.search(
            r"매출|총액|sell\s*out|sellout|점유율|시장\s*규모",
            part,
            flags=re.IGNORECASE,
        )
    )
    if len(patient) != 1 or len(market) != 1:
        return None
    return patient[0], market[0]


def _is_patient_count_intent(question: str) -> bool:
    return bool(
        re.search(r"환자\s*수|유병률|진료\s*인원|상병", question, flags=re.IGNORECASE)
    )
