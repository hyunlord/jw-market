from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections import Counter
import os
import re
from typing import Any
from urllib.parse import urlparse

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.evidence_set_support import public_anchor_id
from jw_chat_agent_poc.service.v4.lossless_contracts import DeterministicRender, EvidenceSet
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|authorization|secret|password)\s*[:=]\s*[^\s,&]+"
)

_INSPECTION_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "hira": (
        "notice_number",
        "source_notice_id",
        "effective_date",
        "source_date",
        "title",
        "brand_name",
        "matching_basis",
        "match_candidates",
        "target",
        "exclusions",
        "administration_frequency",
        "raw_text",
        "sickCd",
        "sickNm",
        "age",
        "age_group",
        "ageRange",
        "year",
        "sex",
        "patient_count",
        "value",
        "inpatOpat",
        "ptntCnt",
        "rvdInsupBrdnAmt",
        "rvdRpeTamtAmt",
        "specCnt",
        "vstDdcnt",
        "units",
        "sexBreakdown",
        "requested_year",
    ),
    "mart": (
        "brand", "brand_name", "period", "year", "month", "sales",
        "sales_krw", "sales_value", "market_share", "share", "rank",
        "growth_rate", "company",
    ),
    "nedrug": (
        "item_name", "product_name", "entp_name", "manufacturer_name",
        "main_item_ingr", "ingredient", "permit_date", "status",
    ),
    "clinicaltrials": (
        "nct_id", "nctId", "status", "phase", "phases", "brief_title",
        "official_title", "title",
    ),
    "patent": (
        "patent_no", "PATENT_NO", "patent_number", "application_number",
        "status", "patentee", "assignee", "expiry_date",
    ),
    "web": ("title", "media", "source", "published_date", "date", "url"),
    "document": (
        "file_name", "document_name", "page", "page_number", "slide",
        "snippet", "quote", "content",
    ),
    "openfda": (
        "application_number", "brand_name", "generic_name",
        "manufacturer_name", "status",
    ),
}


def build_inspection_detail(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    evidence_sets: Sequence[EvidenceSet],
    rendered: DeterministicRender,
    *,
    expansion: Mapping[str, Any] | None = None,
    answer_text: str = "",
) -> dict[str, Any]:
    rendered_ids = {
        record_id for node in rendered.nodes for record_id in node.record_ids
    }
    narrated_ids = {
        str(argument.get("record_id"))
        for claim in rendered.structured_claims
        for argument in claim.get("arguments", ())
        if isinstance(argument, Mapping) and argument.get("record_id")
    }
    sets_by_source = {item.source: item for item in evidence_sets}
    anchor_counts = Counter(
        record.anchor_id
        for evidence_set in evidence_sets
        for record in evidence_set.records
        if record.anchor_id
    )
    unique_anchor_ids = frozenset(
        anchor_id for anchor_id, count in anchor_counts.items() if count == 1
    )
    source_result_counts: dict[str, int] = {}
    for result in results:
        source_result_counts[result.source] = (
            source_result_counts.get(result.source, 0) + 1
        )
    source_result_indexes: dict[str, int] = {}
    calls = []
    for index, result in enumerate(results, start=1):
        source_result_indexes[result.source] = source_result_indexes.get(result.source, 0) + 1
        source_result_index = source_result_indexes[result.source]
        evidence = sets_by_source.get(result.source)
        evidence_records = _result_evidence_records(
            evidence,
            result.source,
            source_result_index,
            source_result_counts[result.source],
            _raw_records(result.payload) if result.status == "ok" else (),
        )
        source_ids = {record.evidence_id for record in evidence_records}
        raw_records = _raw_records(result.payload) if result.status == "ok" else []
        returned = len(raw_records) or (
            _returned_count(result.payload) if result.status == "ok" else 0
        )
        parsed = len(raw_records) if raw_records else len(evidence_records)
        envelope = parsed if evidence_records or result.evidence is not None else 0
        surfaced = _surfaced_record_count(raw_records, answer_text)
        rendered_count = max(len(source_ids & rendered_ids), surfaced)
        narrated = max(len(source_ids & narrated_ids), surfaced)
        inspection_output, anchor_metrics = _inspection_output(
            raw_records,
            returned,
            source=result.source,
            evidence_records=evidence_records,
            unique_anchor_ids=unique_anchor_ids,
        )
        calls.append(
            {
                "sequence": index,
                "trace_sequence": index,
                "source_label": public_source_label(result.source),
                "status": _public_status(result, returned),
                "elapsed_seconds": round(max(result.elapsed_ms, 0.0) / 1000, 3),
                "request_parameters": _request_parameters(result),
                "output": inspection_output,
                "anchor_binding": anchor_metrics,
                "counts": {
                    "returned": returned,
                    "parsed": parsed,
                    "envelope": envelope,
                    "rendered": rendered_count,
                    "narrated": narrated,
                    **(
                        {"used": _used_chunk_count(result.payload)}
                        if result.source == "document"
                        else {}
                    ),
                },
                **(
                    {"document_names": _document_names(result.payload)}
                    if result.source == "document"
                    else {}
                ),
                "record_accounting": _record_accounting(evidence_records, rendered_ids),
                "unused_count": max(returned - narrated, 0),
                "dropped_count": max(returned - parsed, 0),
                "drop_reasons": _drop_reasons(
                    result,
                    evidence,
                    returned,
                    parsed,
                    envelope,
                    rendered_count,
                    narrated,
                    evidence_records,
                    rendered_ids,
                    narrated_ids,
                    raw_records,
                    answer_text,
                ),
            }
        )
    return {
        "schema": "r12.5.inspect.v1",
        "question": _sanitize(plan.resolved_question),
        "expansion": _sanitize_value(dict(expansion or {})),
        "query_scope": _sanitize_value(
            plan.query_scope.model_dump(mode="json") if plan.query_scope else {}
        ),
        "calls": calls,
        "lane_groups": _lane_groups(calls),
        "trace_correlation": {
            "key": "sequence",
            "matched": len(calls),
            "total": len(calls),
            "rate": 1.0 if calls else 0.0,
        },
    }


def _lane_groups(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for call in calls:
        grouped.setdefault(str(call.get("source_label") or ""), []).append(call)
    output: list[dict[str, Any]] = []
    for source_label, lane_calls in grouped.items():
        if len(lane_calls) < 2:
            continue
        output.append(
            {
                "source_label": source_label,
                "call_count": len(lane_calls),
                "sequences": [int(call["sequence"]) for call in lane_calls],
                "returned": sum(
                    int((call.get("counts") or {}).get("returned") or 0)
                    for call in lane_calls
                ),
                "elapsed_seconds": round(
                    sum(float(call.get("elapsed_seconds") or 0.0) for call in lane_calls),
                    3,
                ),
            }
        )
    return output


def _used_chunk_count(payload: Any) -> int:
    if isinstance(payload, Mapping):
        value = payload.get("used_chunk_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _document_names(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    values = payload.get("document_names")
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _returned_count(payload: Any) -> int:
    if isinstance(payload, Mapping):
        for key in ("records", "rows", "items", "studies"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        calls = payload.get("calls")
        if isinstance(calls, list):
            return sum(_returned_count(call) for call in calls)
        for key in ("totalCount", "total_count", "count"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    if isinstance(payload, list):
        return len(payload)
    return 0


def _raw_records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("records", "rows", "items", "studies"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        calls = payload.get("calls")
        if isinstance(calls, list):
            records: list[Mapping[str, Any]] = []
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                render_data = call.get("render_data")
                nested = _raw_records(render_data)
                if nested:
                    request = (
                        render_data.get("request")
                        if isinstance(render_data, Mapping)
                        else None
                    )
                    inherited_year = (
                        render_data.get("requested_year")
                        if isinstance(render_data, Mapping)
                        else None
                    ) or (request.get("year") if isinstance(request, Mapping) else None)
                    records.extend(
                        {
                            **item,
                            **(
                                {"requested_year": inherited_year}
                                if inherited_year and not item.get("requested_year")
                                else {}
                            ),
                        }
                        for item in nested
                    )
                elif isinstance(render_data, Mapping) and render_data:
                    records.append(render_data)
            return records
        for key in ("payload", "data", "detail", "render_data"):
            nested = _raw_records(payload.get(key))
            if nested:
                return nested
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _result_evidence_records(
    evidence: EvidenceSet | None,
    source: str,
    source_result_index: int,
    source_result_count: int,
    raw_records: Sequence[Mapping[str, Any]],
) -> tuple[Any, ...]:
    if evidence is None:
        return ()
    prefix = f"{source}:{source_result_index}:"
    matched = tuple(
        record for record in evidence.records if record.evidence_id.startswith(prefix)
    )
    if matched:
        return matched
    raw_identifiers = {
        identifier
        for raw_record in raw_records
        for identifier in _public_identifiers(raw_record)
    }
    if raw_identifiers:
        matched = tuple(
            record
            for record in evidence.records
            if raw_identifiers & _public_identifiers(record.payload)
        )
        if matched:
            return matched
    return evidence.records if source_result_count == 1 else ()


_PUBLIC_IDENTIFIER_KEYS = frozenset(
    {
        "nct_id",
        "nctId",
        "patent_no",
        "PATENT_NO",
        "patent_number",
        "application_number",
        "item_name",
        "item_seq",
        "ITEM_SEQ",
        "product_name",
        "brand",
        "sickCd",
        "sickNm",
        "title",
        "brief_title",
        "record_id",
        "document_name",
        "file_name",
    }
)


def _public_identifiers(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _PUBLIC_IDENTIFIER_KEYS and nested not in (None, ""):
                if isinstance(nested, (list, tuple)):
                    identifiers.update(_normalized_identifier(item) for item in nested)
                elif not isinstance(nested, Mapping):
                    identifiers.add(_normalized_identifier(nested))
            elif isinstance(nested, (Mapping, list, tuple)):
                identifiers.update(_public_identifiers(nested))
    elif isinstance(value, (list, tuple)):
        for item in value:
            identifiers.update(_public_identifiers(item))
    identifiers.discard("")
    return identifiers


def _normalized_identifier(value: Any) -> str:
    return "".join(str(value).casefold().split())


def _request_parameters(result: SourceResult) -> dict[str, Any]:
    requests: list[Mapping[str, Any]] = []
    if isinstance(result.payload, Mapping):
        calls = result.payload.get("calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                render_data = call.get("render_data")
                if not isinstance(render_data, Mapping):
                    continue
                request = render_data.get("request")
                if isinstance(request, Mapping) and request:
                    requests.append(request)
    output: dict[str, Any] = {"query": _sanitize(result.query)}
    if requests:
        output["calls"] = _sanitize_value(requests)
    return output


def _inspection_output(
    records: Sequence[Mapping[str, Any]],
    returned: int,
    *,
    source: str,
    evidence_records: Sequence[Any] = (),
    unique_anchor_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, int]]:
    projected: list[dict[str, Any]] = []
    eligible = 0
    assigned = 0
    duplicate = 0
    for record in records:
        candidate_anchors = _record_anchor_candidates(
            record,
            source=source,
            evidence_records=evidence_records,
        )
        if candidate_anchors:
            eligible += 1
        unique = candidate_anchors & unique_anchor_ids
        anchor_id = min(unique) if len(unique) == 1 else None
        if anchor_id:
            assigned += 1
        elif candidate_anchors:
            duplicate += 1
        projected.append(_inspection_record(record, source=source, anchor_id=anchor_id))
    displayed: list[dict[str, Any]] = []
    displayed_indexes: dict[str, int] = {}
    for item in projected:
        dedupe_value = {key: value for key, value in item.items() if key != "anchor_id"}
        dedupe_key = repr(_stable_value(dedupe_value))
        previous_index = displayed_indexes.get(dedupe_key)
        if previous_index is None:
            displayed_indexes[dedupe_key] = len(displayed)
            displayed.append(dict(item))
        else:
            displayed[previous_index]["duplicate_count"] = (
                int(displayed[previous_index].get("duplicate_count") or 1) + 1
            )
            count = displayed[previous_index]["duplicate_count"]
            displayed[previous_index]["duplicate_label"] = f"동일 항목 {count}건"
    metrics = {
        "eligible": eligible,
        "assigned": assigned,
        "duplicate": duplicate,
        "unassigned": len(records) - assigned,
    }
    return {
        "returned": returned,
        "displayed_record_count": len(displayed),
        "duplicate_records_collapsed": len(projected) - len(displayed),
        "records": displayed,
    }, metrics


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple((key, _stable_value(nested)) for key, nested in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable_value(item) for item in value)
    return value


def _inspection_record(
    record: Mapping[str, Any],
    *,
    source: str,
    anchor_id: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "identifiers": sorted(_display_identifiers(record)) or ["식별자 없음"]
    }
    if anchor_id:
        output["anchor_id"] = anchor_id
    for field in _INSPECTION_OUTPUT_FIELDS.get(source, ()):
        value = record.get(field)
        if value not in (None, "", [], {}):
            output[field] = (
                _sanitize_multiline(value)
                if field == "raw_text" and isinstance(value, str)
                else _sanitize_value(value)
            )
    if source != "clinicaltrials":
        return output
    title = str(record.get("brief_title") or record.get("official_title") or "").strip()
    interventions = _inspection_text_list(record.get("interventions"), preferred_key="name")
    sponsor = str(record.get("sponsor") or "").strip()
    relevance_status = str(record.get("relevance_status") or "").strip()
    if title:
        output["title"] = _sanitize(title)
    if interventions:
        output["interventions"] = [_sanitize(item) for item in interventions]
    if sponsor:
        output["sponsor"] = _sanitize(sponsor)
    if relevance_status:
        output["relevance_status"] = _sanitize(relevance_status)
    return output


def _record_anchor_candidates(
    record: Mapping[str, Any],
    *,
    source: str,
    evidence_records: Sequence[Any],
) -> frozenset[str]:
    semantic_anchor = public_anchor_id(source, record)
    raw_identifiers = _public_identifiers(record)
    candidates: set[str] = set()
    for evidence_record in evidence_records:
        anchor_id = getattr(evidence_record, "anchor_id", None)
        if not anchor_id:
            continue
        if semantic_anchor and anchor_id == semantic_anchor:
            candidates.add(anchor_id)
            continue
        if raw_identifiers & _public_identifiers(evidence_record.payload):
            candidates.add(anchor_id)
    return frozenset(candidates)


def _inspection_text_list(value: Any, *, preferred_key: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    for item in value:
        candidate = item.get(preferred_key) if isinstance(item, Mapping) else item
        text = str(candidate or "").strip()
        if text:
            output.append(text)
    return list(dict.fromkeys(output))


def _display_identifiers(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _PUBLIC_IDENTIFIER_KEYS and nested not in (None, ""):
                if isinstance(nested, (list, tuple)):
                    identifiers.update(str(item).strip() for item in nested if str(item).strip())
                elif not isinstance(nested, Mapping):
                    identifiers.add(str(nested).strip())
            elif isinstance(nested, (Mapping, list, tuple)):
                identifiers.update(_display_identifiers(nested))
    elif isinstance(value, (list, tuple)):
        for item in value:
            identifiers.update(_display_identifiers(item))
    identifiers.discard("")
    return identifiers


_PRIMARY_VALUE_KEYS = frozenset(
    {
        "ptntCnt",
        "value",
        "sales",
        "sales_value",
        "market_share",
        "patient_count",
        "enrollment",
    }
)
_IDENTIFIER_KEYS = frozenset(
    {
        "sickCd",
        "sickNm",
        "nct_id",
        "nctId",
        "patent_no",
        "patent_number",
        "item_name",
        "brand",
        "title",
        "brief_title",
    }
)


def _surfaced_record_count(records: Sequence[Mapping[str, Any]], answer_text: str) -> int:
    if not answer_text:
        return 0
    return sum(1 for record in records if _record_is_surfaced(record, answer_text))


def _record_is_surfaced(record: Mapping[str, Any], answer_text: str) -> bool:
    primary_values = [
        record.get(key)
        for key in _PRIMARY_VALUE_KEYS
        if record.get(key) not in (None, "")
    ]
    if primary_values:
        return any(_answer_contains_value(answer_text, value) for value in primary_values)
    identifiers = [
        record.get(key)
        for key in _IDENTIFIER_KEYS
        if record.get(key) not in (None, "")
    ]
    return any(_answer_contains_value(answer_text, value) for value in identifiers)


def _answer_contains_value(answer_text: str, value: Any) -> bool:
    needle = "".join(str(value).casefold().replace(",", "").split())
    haystack = "".join(answer_text.casefold().replace(",", "").split())
    return bool(needle) and needle in haystack


def _public_status(result: SourceResult, returned: int) -> str:
    if result.status == "ok":
        return "완료" if returned else "성공+0건"
    if result.status == "empty":
        return "성공+0건"
    if result.status in {"timeout", "deadline_exceeded"}:
        return "미완료"
    if result.status == "parse_error":
        return "변환 실패"
    return "실패"


def _drop_reasons(
    result: SourceResult,
    evidence: EvidenceSet | None,
    returned: int,
    parsed: int,
    envelope: int,
    rendered: int,
    narrated: int,
    evidence_records: Sequence[Any],
    rendered_ids: set[str],
    narrated_ids: set[str],
    raw_records: Sequence[Mapping[str, Any]],
    answer_text: str,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    reasons.extend(_relevance_drop_reasons(evidence))
    if returned > parsed:
        reasons.append(
            {
                "stage": "parsing",
                "count": returned - parsed,
                "reason": "검증 가능한 레코드로 변환되지 않음",
                "record_ids": [],
            }
        )
    if parsed > envelope:
        reasons.append(
            {
                "stage": "envelope",
                "count": parsed - envelope,
                "reason": "동일 호출 내 원시 레코드가 근거 묶음으로 결합됨",
                "record_ids": [],
            }
        )
    if envelope > rendered:
        count = envelope - rendered
        reasons.append(
            {
                "stage": "render",
                "count": count,
                "reason": "현재 답변 표면에 배치되지 않음",
                "record_ids": _bounded_drop_identifiers(
                    count,
                    raw_records,
                    answer_text,
                    evidence_records,
                    excluded_ids=rendered_ids,
                ),
            }
        )
    if rendered > narrated:
        count = rendered - narrated
        reasons.append(
            {
                "stage": "narrative",
                "count": count,
                "reason": "표에는 있으나 서술에 직접 등장하지 않음",
                "record_ids": _bounded_drop_identifiers(
                    count,
                    raw_records,
                    answer_text,
                    evidence_records,
                    included_ids=rendered_ids,
                    excluded_ids=narrated_ids,
                ),
            }
        )
    if result.status != "ok" and result.notice:
        reasons.append(
            {
                "stage": "retrieval",
                "count": 0,
                "reason": _sanitize(result.notice),
                "record_ids": [],
            }
        )
    return reasons


_DEFAULT_OMITTED_IDENTIFIER_LIMIT = 200


def _omitted_identifier_limit() -> int:
    raw = os.environ.get(
        "CHAT_V4_INSPECTION_OMITTED_IDENTIFIER_LIMIT",
        str(_DEFAULT_OMITTED_IDENTIFIER_LIMIT),
    )
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_OMITTED_IDENTIFIER_LIMIT
    return value if value > 0 else _DEFAULT_OMITTED_IDENTIFIER_LIMIT


def _record_accounting(
    evidence_records: Sequence[Any],
    rendered_ids: set[str],
) -> dict[str, Any]:
    """Per-lane ledger proving invariant 2 without shipping record payloads.

    Invariant 2 allows a record to leave the answer surface as long as it is
    still reachable in the inspection panel. Since R13-B' stopped transporting
    record payloads, that claim became unverifiable from the client. The ledger
    below closes twice — received == rendered + omitted, and
    omitted == identified + unidentified — so a silently dropped record shows up
    as an arithmetic break rather than an absence nobody can see.
    """
    limit = _omitted_identifier_limit()
    omitted = [
        record for record in evidence_records if record.evidence_id not in rendered_ids
    ]
    identifiers: list[str] = []
    without_identifier = 0
    for record in omitted:
        display = sorted(_display_identifiers(record.payload))
        if display:
            identifiers.append(_sanitize(display[0]))
        else:
            # Counted, never invented.
            without_identifier += 1
    return {
        "received": len(evidence_records),
        "rendered": len(evidence_records) - len(omitted),
        "omitted": len(omitted),
        "omitted_identifiers": identifiers[:limit],
        "omitted_identifiers_truncated": len(identifiers) > limit,
        "omitted_without_identifier": without_identifier,
        "identifier_limit": limit,
    }


def _bounded_drop_identifiers(
    count: int,
    raw_records: Sequence[Mapping[str, Any]],
    answer_text: str,
    evidence_records: Sequence[Any],
    *,
    included_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
) -> list[str]:
    identifiers = [
        _drop_identifier(record)
        for record in raw_records
        if not _record_is_surfaced(record, answer_text)
    ]
    for record in evidence_records:
        if included_ids is not None and record.evidence_id not in included_ids:
            continue
        if excluded_ids is not None and record.evidence_id in excluded_ids:
            continue
        identifiers.append(
            sorted(_display_identifiers(record.payload))[0]
            if _display_identifiers(record.payload)
            else "식별자 없음"
        )
    bounded = identifiers[:count]
    return [*bounded, *("식별자 없음" for _ in range(count - len(bounded)))]


def _relevance_drop_reasons(evidence: EvidenceSet | None) -> list[dict[str, Any]]:
    if evidence is None or not evidence.coverage.records_excluded_by_relevance:
        return []
    grouped: dict[str, list[str]] = {}
    for manifest in evidence.query_manifest:
        exclusions = manifest.get("relevance_exclusions")
        if not isinstance(exclusions, list):
            continue
        for exclusion in exclusions:
            if not isinstance(exclusion, Mapping):
                continue
            reason = _sanitize(str(exclusion.get("reason") or "질문 관련성 조건을 충족하지 않음"))
            grouped.setdefault(reason, []).append(_drop_identifier(exclusion))
    if grouped:
        reasons = []
        for reason, record_ids in sorted(grouped.items()):
            unique_ids = sorted(dict.fromkeys(record_ids))
            reasons.append({
                "stage": "relevance",
                "count": len(unique_ids),
                "reason": reason,
                "record_ids": unique_ids,
            })
        return reasons
    return [
        {
            "stage": "relevance",
            "count": evidence.coverage.records_excluded_by_relevance,
            "reason": "질문 관련성 조건을 충족하지 않음",
            "record_ids": ["식별자 없음"],
        }
    ]


def _drop_identifier(record: Mapping[str, Any]) -> str:
    for key in (
        "nct_id",
        "nctId",
        "patent_no",
        "patent_number",
        "application_number",
        "url",
        "title",
        "brief_title",
        "brand",
    ):
        value = record.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple)):
            return _sanitize(str(value))
    return "식별자 없음"


def _sanitize(value: str) -> str:
    text = _SECRET_RE.sub("민감값 제거", " ".join(str(value or "").split()))
    return _URL_RE.sub(_safe_url, text)


def _sanitize_multiline(value: str) -> str:
    return "\n".join(_sanitize(line) for line in value.splitlines())


def _safe_url(match: re.Match[str]) -> str:
    url = match.group(0)
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith(".svc") or ".svc." in host or host in {"localhost", "127.0.0.1"}:
        return "내부 조회 경로"
    return url


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(nested)
            for key, nested in value.items()
            if not re.search(r"(?i)(?:secret|token|password|api[_-]?key|sql|url)", str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return _sanitize(value) if isinstance(value, str) else value
