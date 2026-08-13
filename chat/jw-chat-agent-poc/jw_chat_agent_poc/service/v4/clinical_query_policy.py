from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
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


def prepare_resolved_clinical_requests(
    query_resolutions: Iterable[tuple[str, Any]],
    planner_concepts: Iterable[ClinicalTrialConcept],
    *,
    scope_query: str,
) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
    planner_items = tuple(planner_concepts)
    stable_scope = _explicit_scope(scope_query, planner_items)
    prepared: dict[tuple[str, str], tuple[str, ClinicalTrialConcept]] = {}
    resolved_concepts: set[str] = set()
    for query, resolution in query_resolutions:
        decision = resolver_first_clinical_concepts(query, resolution, stable_scope)
        for concept in decision.concepts:
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
        prepared.setdefault(("planner", concept_key), (scope_query, scoped_concept))
    return tuple(prepared.values())


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
        molecules = _normalized_latin_values(getattr(resolution, "molecule_en", ()))
        canonical = str(getattr(resolution, "canonical_brand", "") or "").strip()
        if molecules:
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
                    brands=(canonical,) if canonical else (),
                    search_area="intervention",
                    match="both" if len(molecules) > 1 else "any",
                    countries=planner_countries,
                    statuses=planner_statuses,
                    source_queries=(query,),
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

    unique = tuple(dict((_concept_key(item), item) for item in concepts).values())
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
    if not statuses:
        statuses.extend(DEFAULT_ACTIVE_CLINICAL_STATUSES)
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
