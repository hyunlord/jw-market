from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.document_lane import (
    canonical_file_lane,
    document_record_lane,
)
from jw_chat_agent_poc.service.v4.evidence_set_support import public_anchor_id
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.patent import parse_pms_period
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
        "CLASS_NO", "CONT_QY", "DOMESTIC_END_DATE", "DOMESTIC_INVN_NM",
        "DOMESTIC_LWST_YN", "DOMESTIC_PATENT_NO", "DOMESTIC_PATENT_STATUS",
        "ENTP_NAME", "INGR_ENG_NAME", "INGR_NAME", "ITEM_ENG_NAME",
        "ITEM_NAME", "ITEM_SEQ", "PAGE_GB_NM", "PATENTEE",
        "PATENT_GB_CODE", "PMS_END_DATE", "SHAPE",
    ),
    "web": ("title", "media", "source", "published_date", "date", "url"),
    "document": (
        "file_name", "document_name", "file_type", "document_id",
        "page", "page_number", "slide", "section", "content_excerpt",
        "sheet_name", "row_start", "row_end", "columns", "result_excerpt",
        "retrieval_route",
    ),
    "document_rag": (
        "file_name", "document_name", "file_type", "document_id",
        "page", "page_number", "slide", "section", "content_excerpt",
        "retrieval_route", "record_status", "record_journey", "omission_reason",
    ),
    "document_sql": (
        "file_name", "document_name", "file_type", "document_id",
        "sheet_name", "row_start", "row_end", "columns", "result_excerpt",
        "retrieval_route", "record_status", "record_journey", "omission_reason",
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
    used_record_ids: Sequence[str] = (),
) -> dict[str, Any]:
    rendered_ids = {
        record_id for node in rendered.nodes for record_id in node.record_ids
    }
    document_used_ids = frozenset((*rendered_ids, *used_record_ids))
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
        if result.source == "document":
            source_result_indexes[result.source] = (
                source_result_indexes.get(result.source, 0) + 1
            )
            raw_records = _raw_records(result.payload) if result.status == "ok" else []
            evidence_records = _result_evidence_records(
                sets_by_source.get(result.source),
                result.source,
                source_result_indexes[result.source],
                source_result_counts[result.source],
                raw_records,
            )
            calls.extend(
                _document_inspection_calls(
                    result,
                    rendered_ids=document_used_ids,
                    start_sequence=len(calls) + 1,
                    evidence_records=evidence_records,
                )
            )
            continue
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
            rendered_ids=frozenset(rendered_ids),
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


def build_document_inspection_detail(
    question: str,
    result: SourceResult,
    *,
    rendered_record_ids: Sequence[str],
) -> dict[str, Any]:
    """Project the deterministic file lane through the standard inspection schema."""

    calls = _document_inspection_calls(
        result,
        rendered_ids=frozenset(rendered_record_ids),
        start_sequence=1,
    )
    return {
        "schema": "r12.5.inspect.v1",
        "question": _sanitize(question),
        "expansion": {},
        "query_scope": {},
        "calls": calls,
        "lane_groups": [],
        "trace_correlation": {
            "key": "sequence", "matched": len(calls), "total": len(calls),
            "rate": 1.0 if calls else 0.0,
        },
    }


def _document_inspection_calls(
    result: SourceResult,
    *,
    rendered_ids: frozenset[str],
    start_sequence: int,
    evidence_records: Sequence[EvidenceRecord] | None = None,
) -> list[dict[str, Any]]:
    raw_records = _raw_records(result.payload) if result.status == "ok" else []
    accounting = (
        result.payload.get("route_accounting", {})
        if isinstance(result.payload, Mapping)
        else {}
    )
    tool_details = (
        result.payload.get("file_tool_details", {})
        if isinstance(result.payload, Mapping)
        else {}
    )
    calls: list[dict[str, Any]] = []
    for lane in ("document_rag", "document_sql"):
        route = accounting.get(lane, {}) if isinstance(accounting, Mapping) else {}
        planned = isinstance(route, Mapping) and route.get("planned") is True
        inventory_failure = (
            route.get("inventory_failure") if isinstance(route, Mapping) else None
        )
        execution_failure = (
            route.get("execution_failure") if isinstance(route, Mapping) else None
        )
        lane_failure = inventory_failure or execution_failure
        failure_class = (
            str(lane_failure.get("failure_class") or "").strip()
            if isinstance(lane_failure, Mapping)
            else ""
        )
        lane_records = [record for record in raw_records if document_record_lane(record) == lane]
        lane_evidence_records = (
            tuple(
                record
                for record in evidence_records
                if document_record_lane(record.payload) == lane
            )
            if evidence_records is not None
            else tuple(
                EvidenceRecord(
                    evidence_id=str(record.get("record_id") or f"{lane}:{index}"),
                    source="document",
                    result_kind="document_chunk",
                    payload=dict(record),
                )
                for index, record in enumerate(lane_records, start=1)
            )
        )
        output, anchor_metrics = _inspection_output(
            lane_records,
            len(lane_records),
            source=lane,
            evidence_records=lane_evidence_records,
            rendered_ids=rendered_ids,
        )
        used_ids = {
            record.evidence_id for record in lane_evidence_records
        } & rendered_ids
        raw_detail = tool_details.get(lane) if isinstance(tool_details, Mapping) else None
        detail = raw_detail if isinstance(raw_detail, Mapping) else {}
        request_parameters = _file_request_parameters(result, lane, lane_records, detail)
        output.update(
            _file_inspection_output(
                lane,
                lane_records,
                lane_evidence_records,
                rendered_ids,
                detail,
            )
        )
        sequence = start_sequence + len(calls)
        calls.append(
            {
                "sequence": sequence,
                "trace_sequence": sequence,
                "tool": lane,
                "lane_id": canonical_file_lane(lane),
                "source_label": public_source_label(lane),
                "state": (
                    "no_document"
                    if failure_class == "no_document"
                    else "timeout"
                    if failure_class == "timeout"
                    else "failed"
                    if failure_class
                    else "success"
                    if planned
                    else "unplanned"
                ),
                "status": (
                    "세션에 연결된 문서를 찾지 못했습니다."
                    if failure_class == "no_document"
                    else "응답 시간 초과"
                    if failure_class == "timeout"
                    else "실행 실패"
                    if failure_class
                    else "성공"
                    if lane_records
                    else "성공+0건"
                    if planned
                    else "해당 파일 없음"
                ),
                "elapsed_seconds": round(max(result.elapsed_ms, 0.0) / 1000, 3),
                "request_parameters": request_parameters,
                "output": output,
                "anchor_binding": anchor_metrics,
                "counts": {
                    "returned": len(lane_records), "parsed": len(lane_records),
                    "envelope": len(lane_evidence_records),
                    "rendered": len(used_ids),
                    "narrated": len(used_ids), "used": len(used_ids),
                },
                "document_names": list(
                    dict.fromkeys(
                        str(record.get("document_name") or record.get("file_name") or "")
                        for record in lane_records
                    )
                ),
                "record_accounting": _record_accounting(
                    lane_evidence_records, rendered_ids
                ),
                "unused_count": max(len(lane_records) - len(used_ids), 0),
                "dropped_count": 0,
                "drop_reasons": [],
            }
        )
    return calls


def _file_request_parameters(
    result: SourceResult,
    lane: str,
    records: Sequence[Mapping[str, Any]],
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    if lane == "document_rag":
        document_ids = list(
            dict.fromkeys(
                record.get("document_id")
                for record in records
                if record.get("document_id") is not None
            )
        )
        sheet_ids = list(
            dict.fromkeys(
                str(record.get("sheet_name"))
                for record in records
                if record.get("sheet_name")
            )
        )
        return _sanitize_file_detail(
            {
                "query": str(detail.get("query") or result.query),
                "queries": list(detail.get("queries") or (detail.get("query") or result.query,)),
                "document_ids": document_ids,
                "sheet_ids": sheet_ids,
                "limit": detail.get("top_k"),
                "top_k": detail.get("top_k"),
                "top_k_source": detail.get("top_k_source"),
            }
        )
    output: dict[str, Any] = {"query": _sanitize(result.query)}
    if detail.get("executed_sql"):
        output["executed_sql"] = detail["executed_sql"]
    if detail.get("display_sql"):
        output["display_sql"] = detail["display_sql"]
    if isinstance(detail.get("table_mapping"), (list, tuple)):
        output["table_mapping"] = detail["table_mapping"]
    return _sanitize_file_detail(output)


def _file_inspection_output(
    lane: str,
    records: Sequence[Mapping[str, Any]],
    evidence_records: Sequence[EvidenceRecord],
    rendered_ids: frozenset[str],
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    if lane == "document_rag":
        chunks = []
        for record in records[: _file_detail_chunk_limit()]:
            matching_ids = _matching_evidence_ids(record, evidence_records)
            chunks.append(
                {
                    "document_name": record.get("document_name") or record.get("file_name"),
                    "document_id": record.get("document_id"),
                    "chunk_id": record.get("chunk_id"),
                    "record_id": record.get("record_id"),
                    "source_chunk_index": record.get("source_chunk_index"),
                    "page": record.get("page"),
                    "slide_number": record.get("slide_number"),
                    "sheet_name": record.get("sheet_name"),
                    "section": record.get("section"),
                    "score": record.get("score"),
                    "score_kind": record.get("score_kind"),
                    "similarity_score": record.get("similarity_score"),
                    "distance": record.get("distance"),
                    "content_excerpt": str(record.get("content") or "")[: _file_detail_chunk_chars()],
                    "selected": bool(matching_ids & rendered_ids),
                }
            )
        return _sanitize_file_detail(
            {
                "received_chunk_count": len(records),
                "answer_used_count": sum(
                    bool(_matching_evidence_ids(record, evidence_records) & rendered_ids)
                    for record in records
                ),
                "chunks": chunks,
                "failure_reason": detail.get("failure_reason") if not records else None,
            }
        )
    allowed = {
        key: detail[key]
        for key in (
            "columns",
            "rows",
            "total_row_count",
            "aggregate_values",
            "aggregation_summary",
            "error",
        )
        if key in detail
    }
    rows = allowed.get("rows")
    if isinstance(rows, (list, tuple)):
        allowed["rows"] = rows[: _positive_env_int("FILE_DETAIL_ROW_LIMIT", 10)]
    return _sanitize_file_detail(allowed)


def _file_detail_chunk_chars() -> int:
    return min(2_400, _positive_env_int("FILE_DETAIL_CHUNK_CHARS", 2_400))


def _file_detail_chunk_limit() -> int:
    return min(20, _positive_env_int("FILE_DETAIL_CHUNK_LIMIT", 20))


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _sanitize_file_detail(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_file_detail(nested)
            for key, nested in value.items()
            if not re.search(r"(?i)(?:secret|token|password|api[_-]?key)", str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_file_detail(item) for item in value]
    return _sanitize_preserving_spacing(value) if isinstance(value, str) else value


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
        for key in ("render_data", "payload"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                nested_count = _returned_count(nested)
                if nested_count:
                    return nested_count
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
        "brand_name",
        "generic_name",
        "dailymed_url",
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
    rendered_ids: frozenset[str] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    projected: list[dict[str, Any]] = []
    exposed_evidence_ids: set[str] = set()
    eligible = 0
    assigned = 0
    duplicate = 0
    evidence_anchor_ids, identifier_anchors = _evidence_anchor_lookup(evidence_records)
    for record in records:
        candidate_anchors = _record_anchor_candidates(
            record,
            source=source,
            evidence_anchor_ids=evidence_anchor_ids,
            identifier_anchors=identifier_anchors,
        )
        if candidate_anchors:
            eligible += 1
        unique = candidate_anchors & unique_anchor_ids
        anchor_id = min(unique) if len(unique) == 1 else None
        if anchor_id:
            assigned += 1
        elif candidate_anchors:
            duplicate += 1
        item = _inspection_record(record, source=source, anchor_id=anchor_id)
        matching_ids = _matching_evidence_ids(record, evidence_records)
        exposed_evidence_ids.update(matching_ids)
        if len(matching_ids) == 1:
            item["evidence_id"] = next(iter(matching_ids))
        elif matching_ids:
            item["evidence_refs"] = [
                {"evidence_id": evidence_id}
                for evidence_id in sorted(matching_ids)
            ]
        if source in {"document", "document_rag", "document_sql"} and rendered_ids is not None:
            used = bool(matching_ids & rendered_ids)
            item["record_status"] = "사용됨" if used else "탈락"
            item["record_journey"] = ["검색됨", "후보", item["record_status"]]
            if not used:
                item["omission_reason"] = (
                    "현재 답변 표면에 배치되지 않음" if matching_ids else "사유 미기록"
                )
        projected.append(item)
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
    output = {
        "returned": returned,
        "displayed_record_count": len(displayed),
        "duplicate_records_collapsed": len(projected) - len(displayed),
        "records": displayed,
        "evidence_refs": [
            {"evidence_id": evidence_id}
            for evidence_id in sorted(exposed_evidence_ids)
        ],
    }
    if source == "patent":
        output["returned_scope_label"] = "검사 반환"
        output["displayed_scope_label"] = "조회 상세 표시"
    return output, metrics


def _matching_evidence_ids(
    record: Mapping[str, Any],
    evidence_records: Sequence[Any],
) -> frozenset[str]:
    record_id = str(record.get("record_id") or "").strip()
    if record_id:
        exact = frozenset(
            evidence.evidence_id
            for evidence in evidence_records
            if str(evidence.payload.get("record_id") or "").strip() == record_id
        )
        if exact:
            return exact
    identifiers = _public_identifiers(record)
    return frozenset(
        evidence.evidence_id
        for evidence in evidence_records
        if identifiers & _public_identifiers(evidence.payload)
    )


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
        if source == "patent":
            output[field] = (
                _sanitize_value(value)
                if value not in (None, "", [], {})
                else "원천 미제공"
            )
            continue
        if value not in (None, "", [], {}):
            if field == "sheet_name" and isinstance(value, str):
                output[field] = _sanitize_preserving_spacing(value)
            else:
                output[field] = (
                    _sanitize_multiline(value)
                    if field == "raw_text" and isinstance(value, str)
                    else _sanitize_value(value)
                )
    if source == "patent":
        pms_period = parse_pms_period(record.get("PMS_END_DATE"))
        output["pms_period_start"] = pms_period["start"] or "원천 미제공"
        output["pms_period_end"] = pms_period["end"] or "원천 미제공"
        output["pms_period_format"] = pms_period["format"]
        if pms_period["format"] == "invalid":
            output["pms_period_notice"] = "파싱 불가 형식으로 원문을 보존했습니다"
        output["listing_label"] = (
            "등재특허 아님"
            if str(record.get("PAGE_GB_NM") or "").strip() == "기타특허"
            else "등재특허"
        )
        return output
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
    evidence_anchor_ids: frozenset[str],
    identifier_anchors: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    semantic_anchor = public_anchor_id(source, record)
    raw_identifiers = _public_identifiers(record)
    candidates = {
        anchor_id
        for identifier in raw_identifiers
        for anchor_id in identifier_anchors.get(identifier, ())
    }
    if semantic_anchor and semantic_anchor in evidence_anchor_ids:
        candidates.add(semantic_anchor)
    return frozenset(candidates)


def _evidence_anchor_lookup(
    evidence_records: Sequence[Any],
) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    anchor_ids: set[str] = set()
    mutable_identifier_anchors: dict[str, set[str]] = {}
    for evidence_record in evidence_records:
        anchor_id = getattr(evidence_record, "anchor_id", None)
        if not anchor_id:
            continue
        anchor_ids.add(anchor_id)
        for identifier in _public_identifiers(evidence_record.payload):
            mutable_identifier_anchors.setdefault(identifier, set()).add(anchor_id)
    return (
        frozenset(anchor_ids),
        {
            identifier: frozenset(anchors)
            for identifier, anchors in mutable_identifier_anchors.items()
        },
    )


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
            min(_display_identifiers(record.payload))
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


def _sanitize_preserving_spacing(value: str) -> str:
    text = _SECRET_RE.sub("민감값 제거", str(value or "").strip())
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
