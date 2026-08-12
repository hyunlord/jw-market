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
_COMBINED_INTERVENTION_RE = re.compile(
    r"(?:복합|조합|combination|\band\b|\+|(?:^|\s)(?:및|와|과)(?:\s|$))",
    re.IGNORECASE,
)
_ENTITY_SPLIT_RE = re.compile(
    r"\s*[,，]\s*|\s+(?:및|와|과)\s+|(?<=[가-힣A-Za-z0-9])(?:와|과)\s+"
)


@dataclass(frozen=True)
class ClinicalQueryDecision:
    concepts: tuple[ClinicalTrialConcept, ...]
    blocked_reason: str | None = None
    resolver_used: bool = False
    planner_supplemented: bool = False


def query_entity_candidates(query: str) -> tuple[str, ...]:
    if not _ENTITY_SPLIT_RE.search(query):
        return ()
    candidates = []
    for value in _ENTITY_SPLIT_RE.split(query):
        candidate = _QUERY_SUFFIX_RE.sub("", value).strip(" ,:·")
        if candidate and len(candidate) <= 40:
            candidates.append(candidate)
    return tuple(dict.fromkeys(candidates)) if len(candidates) >= 2 else ()


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
        latin_brand = canonical if canonical and not _HANGUL_RE.search(canonical) else ""
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
                    brands=(latin_brand,) if latin_brand else (),
                    search_area="intervention",
                    match=(
                        "both"
                        if len(molecules) > 1 and _COMBINED_INTERVENTION_RE.search(query)
                        else "any"
                    ),
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
