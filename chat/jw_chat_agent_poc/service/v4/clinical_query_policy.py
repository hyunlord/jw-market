from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.service.v4.clinical import compile_clinical_query
from jw_chat_agent_poc.service.v4.contracts import ClinicalTrialConcept

_HANGUL_RE = re.compile(r"[가-힣]")
_QUERY_SUFFIX_RE = re.compile(
    r"\s*(?:매출|점유율|특허|임상(?:현황|시험)?|급여(?:기준)?|현황|"
    r"clinical\s+trials?).*$",
    re.IGNORECASE,
)
_ENTITY_SPLIT_RE = re.compile(
    r"\s*[,，]\s*|\s+(?:및|와|과)\s+|(?<=[가-힣A-Za-z0-9])(?:와|과)\s+"
)
_ATTRIBUTE_TERMS = (
    "효능",
    "효과",
    "안전성",
    "부작용",
    "용법",
    "용량",
    "매출",
    "점유율",
    "특허",
    "임상",
    "급여",
    "환자수",
)
_NON_ENTITY_CANDIDATES = {
    "국내",
    "대한민국",
    "한국",
    "일본",
    "미국",
    "완료",
    "모집 전",
    "모집 중",
    "진행 중",
}
_COUNTRY_SCOPE_PATTERNS = (
    ("Korea", re.compile(r"(?:국내|한국|대한민국|\bKorea\b)", re.IGNORECASE)),
    ("Japan", re.compile(r"(?:일본|日本|\bJapan\b)", re.IGNORECASE)),
)
_NOT_YET_RECRUITING_RE = re.compile(
    r"(?:모집\s*전|\bnot\s+yet\s+recruiting\b)",
    re.IGNORECASE,
)
_RECRUITING_RE = re.compile(
    r"(?:모집\s*중|진행\s*중|\brecruiting\b|\bactive\b|\bongoing\b)",
    re.IGNORECASE,
)
_COMPLETED_RE = re.compile(r"(?:완료|完了|\bcompleted\b)", re.IGNORECASE)
_TERMINATED_RE = re.compile(
    r"(?:종료|중단|\bterminated\b|\bwithdrawn\b)",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(r"(?:과거|이전|\bhistorical\b|\bpast\b)", re.IGNORECASE)
_EXPLICIT_CLASS_EXPANSION_RE = re.compile(
    r"(?:계열(?:\s*전체)?|구성\s*성분|성분별|성분\s*각각|파생\s*제품)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClinicalEntityVariants:
    pattern: re.Pattern[str]
    notation_variants: tuple[str, ...]


_CLINICAL_ENTITY_VARIANTS = (
    ClinicalEntityVariants(
        pattern=re.compile(r"(?:오메가\s*-?\s*3|\bomega\s*-?\s*3\b)", re.IGNORECASE),
        notation_variants=("Omega-3 fatty acids",),
    ),
    ClinicalEntityVariants(
        pattern=re.compile(r"\bfish\s+oil\b", re.IGNORECASE),
        notation_variants=("fish oil",),
    ),
)
_CLINICAL_CLASS_QUERIES: tuple[
    tuple[re.Pattern[str], tuple[str, ...]], ...
] = (
    (
        re.compile(r"(?:오메가\s*-?\s*3|\bomega\s*-?\s*3\b)", re.IGNORECASE),
        (
            "Omega-3 fatty acids",
            "EPA",
            "DHA",
            "Icosapent ethyl",
            "Omega-3-acid ethyl esters",
        ),
    ),
)

DEFAULT_ACTIVE_CLINICAL_STATUSES = (
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
)
HISTORICAL_CLINICAL_STATUSES = (
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
)


@dataclass(frozen=True)
class ClinicalQueryDecision:
    concepts: tuple[ClinicalTrialConcept, ...]
    blocked_reason: str | None = None
    resolver_used: bool = False
    planner_supplemented: bool = False


def deterministic_clinical_entity_requests(
    query: str,
) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
    """Return only audited notation variants for a known explicit substance."""
    if _ct_molecule_expansion_enabled(query):
        return ()
    policy = next(
        (item for item in _CLINICAL_ENTITY_VARIANTS if item.pattern.search(query)),
        None,
    )
    if policy is None:
        return ()
    scope = _explicit_scope(query)
    countries = scope.countries if scope is not None else ()
    statuses = scope.statuses if scope is not None else ()
    return tuple(
        (
            variant,
            ClinicalTrialConcept(
                ingredients=(variant,),
                search_area="intervention",
                match="any",
                countries=countries,
                statuses=statuses,
                source_queries=(variant,),
                expansion_source="entity_variant_dictionary",
                expansion_status="resolved",
                expansion_grade="notation_variant",
            ),
        )
        for variant in policy.notation_variants
    )


def deterministic_clinical_class_requests(
    query: str,
) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
    """Expand an explicitly requested audited class into separate queries."""

    if not _ct_molecule_expansion_enabled(query):
        return ()
    queries = next(
        (items for pattern, items in _CLINICAL_CLASS_QUERIES if pattern.search(query)),
        (),
    )
    return tuple(
        (
            item,
            ClinicalTrialConcept(
                ingredients=(item,),
                search_area="intervention",
                match="any",
                source_queries=(item,),
                expansion_source="entity_variant_dictionary",
                expansion_status="resolved",
                expansion_grade=(
                    "notation_variant" if index == 0 else "composition_component"
                ),
            ),
        )
        for index, item in enumerate(queries)
    )


def prepare_resolved_clinical_requests(
    query_resolutions: Iterable[tuple[str, Any]],
    planner_concepts: Iterable[ClinicalTrialConcept],
    *,
    scope_query: str,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None = None,
) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
    deterministic_requests = deterministic_clinical_entity_requests(scope_query)
    if deterministic_requests:
        return deterministic_requests
    planner_items = tuple(planner_concepts)
    has_pretranslated_explicit_term = any(
        concept.expansion_source == "nedrug_ingredient"
        and concept.expansion_status == "resolved"
        for concept in planner_items
    )
    effective_molecule_fallback = (
        None if has_pretranslated_explicit_term else molecule_fallback
    )
    stable_scope = _explicit_scope(scope_query, planner_items)
    prepared: dict[tuple[str, str], tuple[str, ClinicalTrialConcept]] = {}
    resolved_concepts: set[str] = set()
    for query, resolution in query_resolutions:
        if not _known_entity_is_explicit(query, scope_query):
            continue
        if _ct_molecule_expansion_enabled(scope_query):
            expanded = _expanded_brand_requests(
                query,
                resolution,
                stable_scope,
                molecule_fallback=effective_molecule_fallback,
            )
            for expanded_query, concept in expanded:
                if not _concept_is_transport_safe(concept):
                    continue
                concept_key = _concept_key(concept)
                entity_key = " ".join(expanded_query.split()).casefold()
                prepared.setdefault((entity_key, concept_key), (expanded_query, concept))
                resolved_concepts.add(concept_key)
            continue
        decision = resolver_first_clinical_concepts(query, resolution, stable_scope)
        default_concepts = decision.concepts
        support_source = str(getattr(resolution, "support_source", "") or "")
        if (
            effective_molecule_fallback is not None
            and "mart_brand_molecule" in support_source
        ):
            canonicalized = _expanded_brand_requests(
                query,
                resolution,
                stable_scope,
                molecule_fallback=effective_molecule_fallback,
            )
            if (
                canonicalized
                and canonicalized[0][1].expansion_source == "nedrug_ingredient"
            ):
                canonical_concept = canonicalized[0][1]
                base_ingredient = single_base_mart_ingredient(
                    getattr(resolution, "molecule_en", ()),
                    canonical_concept.ingredients,
                )
                if base_ingredient is not None:
                    canonical_concept = canonical_concept.model_copy(
                        update={
                            "ingredients": (base_ingredient,),
                            "match": "any",
                        }
                    )
                default_concepts = (canonical_concept,)
        for concept in default_concepts:
            if not _concept_is_transport_safe(concept):
                continue
            concept_key = _concept_key(concept)
            entity_key = " ".join(query.split()).casefold()
            prepared.setdefault((entity_key, concept_key), (query, concept))
            resolved_concepts.add(concept_key)
    for planner_concept in planner_items:
        scoped_concept = _apply_explicit_scope(planner_concept, stable_scope)
        if not _concept_is_transport_safe(scoped_concept):
            continue
        if not _concept_is_question_grounded(planner_concept, scope_query):
            continue
        concept_key = _concept_key(scoped_concept)
        if concept_key in resolved_concepts:
            continue
        planner_query = (
            scoped_concept.ingredients[0]
            if scoped_concept.expansion_source == "nedrug_ingredient"
            and scoped_concept.expansion_status == "resolved"
            and scoped_concept.ingredients
            else scope_query
        )
        prepared.setdefault(("planner", concept_key), (planner_query, scoped_concept))
    return tuple(prepared.values())


def _ct_molecule_expansion_enabled(query: str) -> bool:
    return bool(_EXPLICIT_CLASS_EXPANSION_RE.search(query))


def _known_entity_is_explicit(candidate: str, scope_query: str) -> bool:
    """Reject resolver-derived known entities that the user did not ask for."""
    policy = next(
        (item for item in _CLINICAL_ENTITY_VARIANTS if item.pattern.search(candidate)),
        None,
    )
    return policy is None or bool(policy.pattern.search(scope_query))


def _expanded_brand_requests(
    query: str,
    resolution: Any,
    scope: ClinicalTrialConcept | None,
    *,
    molecule_fallback: Callable[[str], tuple[tuple[str, ...], str]] | None,
) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
    canonical = str(getattr(resolution, "canonical_brand", "") or "").strip()
    support_source = str(getattr(resolution, "support_source", "") or "")
    molecules = tuple(
        sorted(
            _normalized_latin_values(getattr(resolution, "molecule_en", ())),
            key=str.casefold,
        )
    )
    mart_molecules: tuple[str, ...] = ()
    expansion_source = "mart_molecule"
    expansion_status = "resolved"
    if "mart_brand_molecule" in support_source:
        mart_molecules = tuple(
            sorted(
                _normalized_latin_values(getattr(resolution, "molecule_en", ())),
                key=str.casefold,
            )
        )
        molecules = mart_molecules or molecules
        expansion_status = "resolved" if molecules else "empty"
        if molecule_fallback is not None:
            fallback_molecules, fallback_status = molecule_fallback(canonical)
            canonical_molecules = _normalized_latin_values(fallback_molecules)
            if (
                fallback_status == "resolved"
                and canonical_molecules
                and (not mart_molecules or len(canonical_molecules) == 1)
            ):
                molecules = canonical_molecules
                expansion_source = "nedrug_ingredient"
                expansion_status = "resolved"
    else:
        expansion_source = "nedrug_ingredient"
        if molecule_fallback is not None:
            fallback_molecules, fallback_status = molecule_fallback(canonical)
            molecules = _normalized_latin_values(fallback_molecules)
            expansion_status = (
                fallback_status
                if fallback_status in {"resolved", "empty", "failed"}
                else "failed"
            )
        elif molecules:
            expansion_status = "resolved"
        else:
            expansion_status = "failed"

    countries = scope.countries if scope is not None else ()
    statuses = scope.statuses if scope is not None else ()
    original = ClinicalTrialConcept(
        ingredients=molecules,
        brands=(canonical,) if canonical else (query,),
        search_area="intervention",
        match="both" if len(molecules) > 1 else "any",
        countries=countries,
        statuses=statuses,
        source_queries=(query,),
        expansion_source=expansion_source,
        expansion_status=expansion_status,
        expansion_grade="notation_variant",
    )
    requests: list[tuple[str, ClinicalTrialConcept]] = [(query, original)]
    for molecule in molecules:
        requests.append(
            (
                molecule,
                ClinicalTrialConcept(
                    ingredients=(molecule,),
                    brands=(canonical,) if canonical else (),
                    search_area="intervention",
                    match="any",
                    countries=countries,
                    statuses=statuses,
                    source_queries=(molecule,),
                    expansion_source=expansion_source,
                    expansion_status=expansion_status,
                    expansion_grade="composition_component",
                ),
            )
        )
    return tuple(requests)


def query_entity_candidates(query: str) -> tuple[str, ...]:
    if not _ENTITY_SPLIT_RE.search(query):
        return ()
    candidates = []
    for value in _ENTITY_SPLIT_RE.split(query):
        candidate = _QUERY_SUFFIX_RE.sub("", value).strip(" ,:·")
        if candidate and len(candidate) <= 40:
            candidates.append(candidate)
    if any(not is_query_entity_candidate(candidate) for candidate in candidates):
        return ()
    return tuple(dict.fromkeys(candidates)) if len(candidates) >= 2 else ()


def is_query_entity_candidate(value: str) -> bool:
    normalized = " ".join(value.split()).strip().casefold()
    if not normalized or normalized in _NON_ENTITY_CANDIDATES:
        return False
    return not any(attribute in normalized for attribute in _ATTRIBUTE_TERMS)


def resolver_first_clinical_concepts(
    query: str,
    resolution: Any | None,
    planner_concept: ClinicalTrialConcept | None,
) -> ClinicalQueryDecision:
    concepts: list[ClinicalTrialConcept] = []
    resolver_used = False
    if resolution is not None:
        canonical = str(getattr(resolution, "canonical_brand", "") or "").strip()
        molecules = _normalized_latin_values(getattr(resolution, "molecule_en", ()))
        if canonical:
            planner_countries = (
                _normalized_latin_values(planner_concept.countries)
                if planner_concept is not None
                else ()
            )
            planner_statuses = (
                _normalized_latin_values(planner_concept.statuses)
                if planner_concept is not None
                else ()
            )
            concepts.append(
                ClinicalTrialConcept(
                    ingredients=molecules,
                    brands=(canonical,),
                    search_area="intervention",
                    match="both" if len(molecules) > 1 else "any",
                    countries=planner_countries,
                    statuses=planner_statuses,
                    source_queries=(query,),
                    expansion_source="mart_molecule" if molecules else "none",
                    expansion_status="resolved",
                    expansion_grade="notation_variant",
                )
            )
            resolver_used = True

    planner_supplemented = False
    if (
        not resolver_used
        and planner_concept is not None
        and _concept_is_transport_safe(planner_concept)
    ):
        concepts.append(planner_concept)
        planner_supplemented = True

    unique = tuple({_concept_key(item): item for item in concepts}.values())
    if unique:
        return ClinicalQueryDecision(
            concepts=unique,
            resolver_used=resolver_used,
            planner_supplemented=planner_supplemented,
        )
    if _HANGUL_RE.search(query) or (
        planner_concept is not None and not _concept_is_transport_safe(planner_concept)
    ):
        return ClinicalQueryDecision(
            concepts=(),
            blocked_reason="unresolved_korean_clinical_query",
        )
    return ClinicalQueryDecision(
        concepts=(planner_concept,) if planner_concept is not None else (),
        planner_supplemented=planner_concept is not None,
    )


def single_base_mart_ingredient(
    mart_values: Iterable[Any],
    canonical_values: Iterable[Any],
) -> str | None:
    canonical = _normalized_latin_values(canonical_values)
    if len(canonical) != 1:
        return None
    canonical_tokens = set(re.findall(r"[a-z0-9]+", canonical[0].casefold()))
    if not canonical_tokens:
        return None
    candidates: list[str] = []
    for value in _normalized_latin_values(mart_values):
        value_tokens = set(re.findall(r"[a-z0-9]+", value.casefold()))
        if value_tokens and value_tokens <= canonical_tokens:
            candidates.append(value)
    if len(candidates) != 1:
        return None
    return candidates[0]


def _normalized_latin_values(values: Iterable[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        item = " ".join(str(value).split()).strip()
        if item and not _HANGUL_RE.search(item):
            normalized.append(item)
    return tuple(dict.fromkeys(normalized))


def _explicit_scope(
    query: str,
    planner_concepts: Iterable[ClinicalTrialConcept] = (),
) -> ClinicalTrialConcept | None:
    countries = [
        country
        for country, pattern in _COUNTRY_SCOPE_PATTERNS
        if pattern.search(query)
    ]
    status_query = _NOT_YET_RECRUITING_RE.sub("", query)
    statuses: list[str] = []
    if _HISTORICAL_RE.search(query):
        statuses.extend(HISTORICAL_CLINICAL_STATUSES)
    elif _NOT_YET_RECRUITING_RE.search(query):
        statuses.append("NOT_YET_RECRUITING")
    if not statuses and _RECRUITING_RE.search(status_query):
        statuses.append("RECRUITING")
    if not statuses and _COMPLETED_RE.search(query):
        statuses.append("COMPLETED")
    if not statuses and _TERMINATED_RE.search(query):
        statuses.extend(("TERMINATED", "WITHDRAWN"))
    question_tokens = _semantic_tokens(query)
    for concept in planner_concepts:
        countries.extend(
            country
            for country in concept.countries
            if _contains_token_sequence(question_tokens, _semantic_tokens(country))
        )
        statuses.extend(
            status
            for status in concept.statuses
            if _contains_token_sequence(question_tokens, _semantic_tokens(status))
        )
    countries = list(dict.fromkeys(countries))
    statuses = list(dict.fromkeys(statuses))
    return ClinicalTrialConcept(
        countries=tuple(countries),
        statuses=tuple(statuses),
        source_queries=(query,),
    )


def clinical_scope_suffix(query: str) -> str:
    scope = _explicit_scope(query)
    if scope is None:
        return ""
    return " ".join((*scope.countries, *scope.statuses))


def _concept_is_question_grounded(
    concept: ClinicalTrialConcept,
    scope_query: str,
) -> bool:
    question_tokens = _semantic_tokens(scope_query)
    if (
        concept.expansion_source == "nedrug_ingredient"
        and concept.expansion_status == "resolved"
        and any(
            _contains_token_sequence(question_tokens, _semantic_tokens(source_query))
            for source_query in concept.source_queries
        )
    ):
        return True
    semantic_terms = (*concept.ingredients, *concept.brands)
    terms = semantic_terms or tuple(
        _QUERY_SUFFIX_RE.sub("", value).strip()
        for value in concept.source_queries
    )
    normalized_terms = tuple(
        _semantic_tokens(normalized)
        for value in terms
        if (normalized := " ".join(str(value).split()).strip())
    )
    return bool(normalized_terms) and all(
        _contains_token_sequence(question_tokens, term_tokens)
        for term_tokens in normalized_terms
    )


def _apply_explicit_scope(
    concept: ClinicalTrialConcept,
    explicit_scope: ClinicalTrialConcept | None,
) -> ClinicalTrialConcept:
    return concept.model_copy(
        update={
            "countries": explicit_scope.countries if explicit_scope is not None else (),
            "statuses": explicit_scope.statuses if explicit_scope is not None else (),
        }
    )


def _semantic_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w-]+", value.casefold()))


def _contains_token_sequence(
    haystack: tuple[str, ...],
    needle: tuple[str, ...],
) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


def _concept_is_transport_safe(concept: ClinicalTrialConcept) -> bool:
    try:
        compiled = compile_clinical_query(concept)
    except ValueError:
        return False
    return not any(_HANGUL_RE.search(str(value)) for value in compiled.parameters.values())


def _concept_key(concept: ClinicalTrialConcept) -> str:
    return json.dumps(
        compile_clinical_query(concept).parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
