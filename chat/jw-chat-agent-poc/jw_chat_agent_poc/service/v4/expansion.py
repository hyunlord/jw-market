from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import re
import unicodedata
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
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
    r"(?<![A-Z0-9])(?P<code>[A-Z]\d{2}(?:\.?\d{1,2})?)(?![A-Z0-9])",
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
) -> ExpansionOutcome:
    interpreted = " ".join(
        (
            question,
            plan.resolved_question,
            *plan.expanded_intents,
            *plan.requested_answer_shape.entities,
        )
    )
    codes = (
        _kcd_codes(question)
        or _kcd_codes(interpreted)
        or disease_kcd_codes(interpreted)
    )
    codes = codes[: configured_entity_limit()]
    # The user's relative period is authoritative when a planner also emits a
    # stale calendar expansion such as "recent 5 years (2021-2025)".
    years = _years(question, observed_on) or _years(interpreted, observed_on)
    updates: dict[str, tuple[str, ...]] = {}
    entity_expansion: dict[str, Any] = {"status": "not_applicable"}
    if codes:
        base = strip_mapped_disease_names(_query_subject(question, codes, years)) or "환자수"
        if years:
            updates["hira"] = tuple(
                f"{code} {base} {year}년" for code in codes for year in years
            )
        else:
            updates["hira"] = tuple(f"{code} {base}" for code in codes)
    elif len(years) > 1:
        for source in plan.answer_sources:
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
    anchor_brands, brand_source = disease_brand_set(interpreted)
    anchor_brands = anchor_brands[: configured_entity_limit()]
    molecule_expansion = _expand_disease_molecules(
        plan,
        question,
        interpreted,
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
    if anchor_brands:
        patent_queries = tuple(f"{brand} 특허현황" for brand in anchor_brands)
        if "특허" in question:
            updates["patent"] = patent_queries
            updates["mart"] = tuple(
                f"{brand} 내부 시장 데이터" for brand in anchor_brands
            )
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
    plan_updates: dict[str, Any] = {}
    if updates:
        plan_updates["tool_queries"] = plan.tool_queries.model_copy(update=updates)
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
            "requests": {
                source: list(queries) for source, queries in sorted(updates.items())
            },
            "entity_expansion": entity_expansion,
            "deterministic": True,
        },
    )


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
        for source in plan.answer_sources
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
    interpreted: str,
    *,
    codes: Sequence[str],
    seed_brands: Sequence[str],
    molecule_reader: Any | None,
) -> dict[str, Any] | None:
    candidates = tuple(
        dict.fromkeys(
            ingredient.strip()
            for concept in plan.clinical_query_specs
            for ingredient in concept.ingredients
            if _is_molecule_candidate(ingredient, question)
        )
    )
    if not (candidates or seed_brands) or not (codes or disease_kcd_codes(interpreted)):
        return None
    if molecule_reader is None:
        from jw_chat_agent_poc.agent_loop.factory import default_brand_molecule_reader

        molecule_reader = default_brand_molecule_reader()
    rows = tuple(molecule_reader.brand_molecules()) if molecule_reader is not None else ()
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
    validated = tuple(
        dict.fromkeys(
            str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
            for row in matched_rows
            if str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
        )
    )
    brands = tuple(
        dict.fromkeys(
            (
                *seed_brands,
                *(
                    str(row.get("brand_name") or "").strip()
                    for row in matched_rows
                    if str(row.get("brand_name") or "").strip()
                ),
            )
        )
    )[: configured_entity_limit()]
    atc4_codes = tuple(
        dict.fromkeys(
            str(row.get("atc4_code") or "").strip().upper()
            for row in matched_rows
            if str(row.get("atc4_code") or "").strip()
        )
    )[: configured_entity_limit()]
    mart_queries = tuple(
        dict.fromkeys(
            (
                *(f"{brand} 내부 시장 데이터" for brand in brands),
                *(f"{molecule} 성분 시장 데이터" for molecule in validated),
                *(f"{atc4} 일반 시장 데이터" for atc4 in atc4_codes),
            )
        )
    )
    external_candidates = tuple(dict.fromkeys((*validated, *candidates)))[
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
            match.group("code").upper().replace(".", "")
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
