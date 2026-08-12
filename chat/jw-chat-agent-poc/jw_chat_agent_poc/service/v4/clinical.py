from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from jw_chat_agent_poc.service.v4.contracts import ClinicalTrialConcept


_FILLER_RE = re.compile(
    r"\b(?:clinical\s+trials?|clinicaltrials|combination|portfolio|overview|status)\b|"
    r"(?:임상\s*시험|임상|시험|현황|포트폴리오|조합|복합제)",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CompiledClinicalQuery:
    query_id: str
    query_type: str
    expression: str
    parameters: dict[str, str | int]
    concept: ClinicalTrialConcept


def compile_clinical_query(concept: ClinicalTrialConcept) -> CompiledClinicalQuery:
    # Ingredient names are the canonical intervention search terms. Requiring a
    # brand in the same AND expression would exclude trials indexed only by INN.
    terms = _clean_terms(concept.ingredients) or _clean_terms(concept.brands)
    if not terms:
        terms = _clean_terms(concept.source_queries)
    if not terms:
        raise ValueError("clinical query concept has no searchable terms")

    operator = " AND " if concept.match == "both" else " OR "
    expression = operator.join(terms)
    query_key = "query.cond" if concept.search_area == "condition" else "query.intr"
    parameters: dict[str, str | int] = {
        query_key: expression,
        "pageSize": 100,
        "countTotal": "true",
    }
    countries = _clean_terms(concept.countries)
    if countries:
        parameters["query.locn"] = " OR ".join(countries)
    statuses = _clean_terms(concept.statuses)
    if statuses:
        parameters["filter.overallStatus"] = "|".join(statuses)

    identity = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CompiledClinicalQuery(
        query_id=f"ctq:{hashlib.sha256(identity.encode()).hexdigest()[:16]}",
        query_type=concept.search_area,
        expression=expression,
        parameters=parameters,
        concept=concept,
    )


def concept_from_query(
    query: str,
    *,
    search_area: str = "intervention",
    matched_terms: Sequence[str] = (),
) -> ClinicalTrialConcept:
    terms = _clean_terms(matched_terms)
    match = "both"
    if not terms:
        cleaned = _clean_text(query)
        split = tuple(
            part.strip()
            for part in re.split(r"\s+(?:AND|and|&)\s+|\s*\+\s*", cleaned)
            if part.strip()
        )
        terms = split if len(split) > 1 else ((cleaned,) if cleaned else ())
        match = "both" if len(split) > 1 else "any"
    return ClinicalTrialConcept(
        ingredients=terms if search_area == "intervention" else (),
        search_area="condition" if search_area == "condition" else "intervention",
        match=match if len(terms) > 1 else "any",
        source_queries=(query,),
    )


def normalize_clinical_study(
    study: Mapping[str, Any],
    *,
    matched_queries: Sequence[str] = (),
) -> dict[str, Any]:
    protocol = _mapping(study.get("protocolSection"))
    identification = _mapping(protocol.get("identificationModule"))
    design = _mapping(protocol.get("designModule"))
    design_info = _mapping(design.get("designInfo"))
    status = _mapping(protocol.get("statusModule"))
    conditions = _mapping(protocol.get("conditionsModule"))
    arms = _mapping(protocol.get("armsInterventionsModule"))
    sponsor_module = _mapping(protocol.get("sponsorCollaboratorsModule"))
    lead_sponsor = _mapping(sponsor_module.get("leadSponsor"))
    locations_module = _mapping(protocol.get("contactsLocationsModule"))

    nct_id = _text(identification.get("nctId") or study.get("NCTId")).upper()
    raw_phases = _string_list(design.get("phases") or study.get("phases"))
    phases = ["PHASE_NA" if value in {"NA", "N/A"} else value for value in raw_phases]
    interventions = [
        name
        for item in _mapping_list(arms.get("interventions"))
        if (name := _text(item.get("name")))
    ]
    comparators = [
        label
        for item in _mapping_list(arms.get("armGroups"))
        if "COMPARATOR" in _text(item.get("type")).upper()
        and (label := _text(item.get("label")))
    ]
    countries = [
        country
        for item in _mapping_list(locations_module.get("locations"))
        if (country := _text(item.get("country")))
    ]
    enrollment = _mapping(design.get("enrollmentInfo"))
    enrollment_value = {
        "count": enrollment.get("count"),
        "type": _text(enrollment.get("type")) or None,
    }
    if enrollment_value["count"] is None and enrollment_value["type"] is None:
        enrollment_value = {"count": None, "type": None}

    return {
        "nct_id": nct_id,
        "brief_title": _text(identification.get("briefTitle") or study.get("briefTitle")),
        "official_title": _text(identification.get("officialTitle")),
        "study_type": _text(design.get("studyType")),
        "phases": phases,
        "overall_status": _text(status.get("overallStatus") or study.get("overallStatus")),
        "conditions": _string_list(conditions.get("conditions")),
        "interventions": list(dict.fromkeys(interventions)),
        "comparators": list(dict.fromkeys(comparators)),
        "sponsor": _text(lead_sponsor.get("name")),
        "enrollment": enrollment_value,
        "start_date": _date_value(status.get("startDateStruct") or status.get("startDate")),
        "primary_completion_date": _date_value(status.get("primaryCompletionDateStruct")),
        "completion_date": _date_value(status.get("completionDateStruct")),
        "last_update_date": _date_value(
            status.get("lastUpdatePostDateStruct")
            or status.get("studyFirstPostDateStruct")
        ),
        "countries": list(dict.fromkeys(countries)),
        "has_results": study.get("hasResults") if isinstance(study.get("hasResults"), bool) else None,
        "matched_query": list(dict.fromkeys(str(value) for value in matched_queries if str(value))),
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
    }


def normalize_clinical_detail(
    detail: Mapping[str, Any],
    *,
    matched_queries: Sequence[str] = (),
    source_url: str = "",
) -> dict[str, Any]:
    nct_id = _text(detail.get("nct_id") or detail.get("nctId")).upper()
    raw_phases = _string_list(detail.get("phases") or detail.get("phase"))
    phases = ["PHASE_NA" if value in {"NA", "N/A"} else value for value in raw_phases]
    raw_enrollment = detail.get("enrollment")
    enrollment = (
        dict(raw_enrollment)
        if isinstance(raw_enrollment, Mapping)
        else {"count": raw_enrollment, "type": None}
    )
    return {
        **dict(detail),
        "nct_id": nct_id,
        "brief_title": _text(detail.get("brief_title") or detail.get("title")),
        "official_title": _text(detail.get("official_title")),
        "study_type": _text(detail.get("study_type")),
        "phases": phases,
        "overall_status": _text(detail.get("overall_status") or detail.get("status")),
        "conditions": _string_list(detail.get("conditions")),
        "interventions": _string_list(detail.get("interventions")),
        "comparators": _string_list(detail.get("comparators")),
        "sponsor": _text(detail.get("sponsor")),
        "enrollment": enrollment,
        "start_date": _date_value(detail.get("start_date")),
        "primary_completion_date": _date_value(detail.get("primary_completion_date")),
        "completion_date": _date_value(detail.get("completion_date")),
        "last_update_date": _date_value(detail.get("last_update_date")),
        "countries": _string_list(detail.get("countries")),
        "has_results": (
            detail.get("has_results")
            if isinstance(detail.get("has_results"), bool)
            else None
        ),
        "matched_query": list(dict.fromkeys(str(value) for value in matched_queries if str(value))),
        "url": _text(detail.get("url")) or source_url,
    }


def merge_clinical_searches(searches: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_nct: dict[str, dict[str, Any]] = {}
    for search in searches:
        records = search.get("records")
        if not isinstance(records, list):
            continue
        for raw in records:
            if not isinstance(raw, Mapping):
                continue
            record = dict(raw)
            nct_id = _text(record.get("nct_id")).upper()
            if not nct_id:
                continue
            matched = _string_list(record.get("matched_query"))
            if nct_id not in by_nct:
                record["matched_query"] = matched
                by_nct[nct_id] = record
                continue
            current = by_nct[nct_id]
            current["matched_query"] = list(
                dict.fromkeys([*_string_list(current.get("matched_query")), *matched])
            )
    return list(by_nct.values())


def _clean_terms(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(cleaned for value in values if (cleaned := _clean_text(value))))


def _clean_text(value: object) -> str:
    text = _FILLER_RE.sub(" ", str(value or ""))
    text = re.sub(r"\b(?:and|or)\b\s*$", "", text, flags=re.IGNORECASE)
    return _SPACE_RE.sub(" ", text).strip(" ,+/|-_")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(_text(item) for item in value if _text(item)))


def _date_value(value: object) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("date"))
    return _text(value)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""
