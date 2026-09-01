from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

import requests

DETAIL_SCHEMA = "jw.detail-on-demand.v1"
DETAIL_OWNER_TRACE_KEY = "_detail_owner_id"
_DETAIL_ARCHIVE_FIELDS = ("inspection_detail", "tool_results", "evidence_catalog")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:api[-_]?key|authorization|cookie|password|secret|token)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_IDENTITY_FIELDS = {
    "nct": ("nct_id", "nctId", "NCTId", "study_id"),
    "patent": (
        "patent_no",
        "patent_number",
        "DOMESTIC_PATENT_NO",
        "KOR_PAT_NO",
        "application_number",
    ),
    "chunk": ("chunk_id", "record_id"),
    "document": ("document_id", "document_name", "file_name"),
    "openfda": (
        "application_number",
        "set_id",
        "id",
        "generic_name",
        "active_ingredient",
        "substance_name",
    ),
    "hira_code": ("sickCd", "sick_cd", "disease_code", "sick_code", "kcd_code"),
    "period": ("period", "year_month", "year", "date"),
    "mart_entity": ("brand", "brand_name", "product_name"),
    "web": ("url",),
    "file": ("file_name", "sheet_name"),
}
_INTERNAL_TRACE_FIELDS = frozenset(
    {
        "record_journey",
        "record_status",
        "retrieval_route",
        "omission_reason",
        "selected",
    }
)
_WEB_SOURCES = {"web", "web_news", "tavily", "tavily_mcp"}
_WEB_EXTRACT_TIMEOUT_S = 12
_WEB_EXTRACT_FAILURE_NOTICE = "본문 미확보 - 제목·URL만 수집"
_SOURCE_RECORD_FIELDS = ("records", "results", "rows", "items", "studies", "chunks")


def archive_trace_for_detail(
    transport_trace: Mapping[str, Any],
    source_trace: Mapping[str, Any],
    *,
    portal_user_id: int | None,
) -> Mapping[str, Any]:
    if portal_user_id is None:
        return source_trace
    archived = deepcopy(dict(transport_trace))
    for field in _DETAIL_ARCHIVE_FIELDS:
        if field in source_trace:
            archived[field] = deepcopy(source_trace[field])
    if "trace_id" in source_trace:
        archived["trace_id"] = source_trace["trace_id"]
    if portal_user_id is not None:
        archived[DETAIL_OWNER_TRACE_KEY] = portal_user_id
    return archived


def attach_detail_contract(trace: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(trace))
    response_id = str(projected.get("trace_id") or "").strip()
    if not response_id:
        return projected
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    inspection = projected.get("inspection_detail")
    calls = inspection.get("calls") if isinstance(inspection, Mapping) else None
    if isinstance(calls, Sequence) and not isinstance(calls, str | bytes):
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                continue
            _append_item(items, seen, f"inspection:{index}", "inspection", call)

    tool_results = projected.get("tool_results")
    if isinstance(tool_results, Sequence) and not isinstance(tool_results, str | bytes):
        for index, result in enumerate(tool_results):
            if not isinstance(result, Mapping):
                continue
            _append_item(items, seen, f"tool:{index}", "tool_result", result)

    for value in _evidence_roots(projected):
        for record in _walk_mappings(value):
            evidence_id = _evidence_id(record)
            if evidence_id:
                _append_item(items, seen, evidence_id, "evidence", record)

    projected["detail_on_demand"] = {
        "schema": DETAIL_SCHEMA,
        "response_id": response_id,
        "items": items,
        "truncation": {
            "silent": False,
            "detail_fetch_required": False,
            "inline_inspection_detail": True,
            "notice": "원문은 항목을 펼칠 때 조회할 수 있습니다.",
        },
    }
    return projected


def resolve_detail_item(
    trace: Mapping[str, Any], item_key: str
) -> dict[str, Any] | None:
    key = item_key.strip()
    if not key:
        return None

    if key.startswith("inspection:"):
        value = _indexed_value(trace.get("inspection_detail"), "calls", key)
        return (
            _detail_payload(key, "inspection", value, contexts=(value,))
            if value is not None
            else None
        )
    if key.startswith("tool:"):
        value = _indexed_sequence_value(trace.get("tool_results"), key)
        return (
            _detail_payload(key, "tool_result", value, contexts=(value,))
            if value is not None
            else None
        )

    positional = _positional_source_record(trace, key)
    if positional is not None:
        identities = _record_identities(positional)
        return _detail_payload(
            key,
            "evidence",
            positional,
            contexts=_related_contexts(trace, identities),
        )

    matches: list[Mapping[str, Any]] = []
    for root in _evidence_roots(trace):
        for record in _walk_mappings(root):
            if _evidence_id(record) == key:
                matches.append(record)
    if not matches:
        return None
    identities = _item_key_identities(key) | {
        identity for record in matches for identity in _record_identities(record)
    }
    if identities:
        for root in _evidence_roots(trace):
            for record in _walk_mappings(root):
                if identities.intersection(_record_identities(record)):
                    matches.append(record)
    value = max(
        matches,
        key=lambda record: (
            len(identities.intersection(_record_identities(record))),
            len(record),
            _record_information_score(record),
        ),
    )
    return _detail_payload(
        key,
        "evidence",
        value,
        contexts=_related_contexts(trace, identities),
    )


def hydrate_web_detail(
    payload: Mapping[str, Any],
    *,
    extractor: Any | None = None,
) -> dict[str, Any]:
    """Fetch web body only after an authenticated detail request."""

    hydrated = deepcopy(dict(payload))
    detail = hydrated.get("detail")
    target = _find_web_detail(detail)
    if target is None or target.get("content_status") == "extract_ready":
        return hydrated
    url = str(target.get("url") or "").strip()
    fetch = extractor or _tavily_extract
    try:
        content = str(fetch(url) or "").strip() if _is_public_url(url) else ""
    except (OSError, RuntimeError, ValueError, requests.RequestException):
        content = ""
    if content:
        target["content_status"] = "extract_ready"
        target["content"] = content
        target.pop("content_notice", None)
    else:
        target["content_status"] = "extract_failed"
        target["content_notice"] = _WEB_EXTRACT_FAILURE_NOTICE
    return hydrated


def compact_inline_detail(trace: Mapping[str, Any]) -> dict[str, Any]:
    projected = attach_detail_contract(trace)
    inspection = projected.get("inspection_detail")
    calls = inspection.get("calls") if isinstance(inspection, Mapping) else None
    if isinstance(calls, list):
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call["detail_ref"] = f"inspection:{index}"
            output = call.get("output")
            if isinstance(output, Mapping):
                call["output"] = (
                    _compact_document_retrieval_output(output)
                    if call.get("lane_id") == "file_vdb"
                    else _compact_mapping(output)
                )
    tool_results = projected.get("tool_results")
    if isinstance(tool_results, list):
        compacted = []
        for index, result in enumerate(tool_results):
            if not isinstance(result, Mapping):
                compacted.append(result)
                continue
            summary = _compact_mapping(result)
            summary["detail_ref"] = f"tool:{index}"
            compacted.append(summary)
        projected["tool_results"] = compacted
    contract = projected.get("detail_on_demand")
    if isinstance(contract, dict):
        contract["truncation"] = {
            "silent": False,
            "detail_fetch_required": True,
            "inline_inspection_detail": False,
            "received_count": len(contract["items"]),
            "shown_count": len(contract["items"]),
            "notice": "원문은 항목을 펼칠 때 조회합니다.",
        }
    return projected


def _compact_document_retrieval_output(output: Mapping[str, Any]) -> dict[str, Any]:
    allowed_chunk_fields = (
        "document_name",
        "document_id",
        "chunk_id",
        "record_id",
        "source_chunk_index",
        "page",
        "slide_number",
        "sheet_name",
        "section",
        "score",
        "score_kind",
        "similarity_score",
        "distance",
        "selected",
    )
    chunks = []
    raw_chunks = output.get("chunks")
    if isinstance(raw_chunks, Sequence) and not isinstance(raw_chunks, str | bytes):
        for raw_chunk in raw_chunks[:20]:
            if not isinstance(raw_chunk, Mapping):
                continue
            chunk = {
                field: deepcopy(raw_chunk[field])
                for field in allowed_chunk_fields
                if field in raw_chunk
            }
            excerpt = str(raw_chunk.get("content_excerpt") or "")
            if excerpt:
                chunk["content_excerpt"] = excerpt[:300]
            chunks.append(chunk)
    return {
        key: deepcopy(output[key])
        for key in ("received_chunk_count", "answer_used_count", "failure_reason")
        if key in output and output[key] not in (None, "")
    } | {"chunks": chunks}


def _append_item(
    items: list[dict[str, Any]],
    seen: set[str],
    item_key: str,
    kind: str,
    value: Mapping[str, Any],
) -> None:
    if item_key in seen:
        return
    seen.add(item_key)
    source = str(
        value.get("source") or value.get("lane") or value.get("tool") or "unknown"
    )
    items.append(
        {
            "item_key": item_key,
            "kind": kind,
            "source": source,
            "identifier": _identifier(value),
            "summary": _summary(value),
        }
    )


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for nested in value:
            yield from _walk_mappings(nested)


def _evidence_id(value: Mapping[str, Any]) -> str:
    for key in ("evidence_id", "record_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _identifier(value: Mapping[str, Any]) -> str:
    for key in (
        "evidence_id",
        "record_id",
        "nct_id",
        "patent_no",
        "DOMESTIC_PATENT_NO",
        "document_id",
        "title",
        "tool",
        "query",
    ):
        candidate = value.get(key)
        if candidate not in (None, ""):
            return str(candidate)
    return ""


def _summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(candidate)
        for key, candidate in value.items()
        if key
        in {
            "source",
            "lane",
            "tool",
            "query",
            "returned",
            "received_count",
            "directly_relevant_count",
            "status",
            "evidence_id",
            "record_id",
            "nct_id",
            "patent_no",
            "DOMESTIC_PATENT_NO",
            "title",
            "brief_title",
            "caption",
        }
    }


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    summary = _summary(value)
    for key in ("records", "items", "results", "rows", "studies"):
        records = value.get(key)
        if isinstance(records, Sequence) and not isinstance(records, str | bytes):
            summary[f"{key}_count"] = len(records)
    return summary


def _indexed_value(root: Any, field: str, item_key: str) -> Mapping[str, Any] | None:
    if not isinstance(root, Mapping):
        return None
    return _indexed_sequence_value(root.get(field), item_key)


def _indexed_sequence_value(root: Any, item_key: str) -> Mapping[str, Any] | None:
    if not isinstance(root, Sequence) or isinstance(root, str | bytes):
        return None
    try:
        index = int(item_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None
    if index < 0 or index >= len(root) or not isinstance(root[index], Mapping):
        return None
    return root[index]


def _positional_source_record(
    trace: Mapping[str, Any], item_key: str
) -> Mapping[str, Any] | None:
    match = re.fullmatch(
        r"([a-z_]+):(\d+):(\d+):(\d+)", item_key, re.IGNORECASE
    )
    if match is None:
        return None
    source = _context_source({"source": match.group(1)})
    result_index = int(match.group(2)) - 1
    call_index = int(match.group(3)) - 1
    record_index = int(match.group(4)) - 1
    if min(result_index, call_index, record_index) < 0:
        return None
    tool_results = trace.get("tool_results")
    if not isinstance(tool_results, Sequence) or isinstance(tool_results, str | bytes):
        return None
    source_results = [
        result
        for result in tool_results
        if isinstance(result, Mapping) and _context_source(result) == source
    ]
    if not source_results:
        return None
    candidate_results = (
        [source_results[result_index]]
        if result_index < len(source_results)
        else source_results
    )
    for result in candidate_results:
        calls = next(
            (
                child
                for record in _walk_mappings(result)
                for child in (record.get("calls"),)
                if isinstance(child, Sequence)
                and not isinstance(child, str | bytes)
            ),
            None,
        )
        if calls is not None and call_index < len(calls):
            for records in _source_record_sequences(calls[call_index]):
                if record_index < len(records):
                    return records[record_index]
        for records in _source_record_sequences(result):
            if record_index < len(records):
                return records[record_index]
    return None


def _source_record_sequences(
    value: Any,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    sequences: list[tuple[Mapping[str, Any], ...]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                str(key) in _SOURCE_RECORD_FIELDS
                and isinstance(child, Sequence)
                and not isinstance(child, str | bytes)
            ):
                records = tuple(item for item in child if isinstance(item, Mapping))
                if records:
                    sequences.append(records)
            sequences.extend(_source_record_sequences(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for child in value:
            sequences.extend(_source_record_sequences(child))
    return tuple(sequences)


def _detail_payload(
    item_key: str,
    kind: str,
    value: Mapping[str, Any],
    *,
    contexts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    detail, hidden_field_count = _public_detail(value)
    public_field_count = len(detail) if isinstance(detail, Mapping) else 0
    source_field_count = len(value)
    internal_trace_fields = _internal_trace_field_paths(value)
    source_responses = _source_responses(contexts)
    detail_input = _detail_input(contexts)
    query_breakdown = _query_breakdown(contexts)
    if query_breakdown:
        detail_input["by_query"] = query_breakdown
    length_hints = _length_hints(detail)
    if source_responses:
        length_hints.update(_length_hints(source_responses, "source_responses"))
    payload = {
        "schema": DETAIL_SCHEMA,
        "item_key": item_key,
        "kind": kind,
        "detail": detail,
        "input": detail_input,
        "output": _detail_output(contexts),
        "field_metadata": {
            "public_field_count": public_field_count,
            "source_provided_field_count": source_field_count,
            "displayed_field_count": public_field_count,
            "hidden_field_count": hidden_field_count,
            "hidden_field_notice": f"내부 필드 {hidden_field_count}개 비표시",
            "internal_trace_fields": internal_trace_fields,
            "missing_fields": _missing_fields(detail),
            "length_hints": length_hints,
        },
        "partial": False,
    }
    if source_responses:
        payload["source_responses"] = source_responses
    return payload


def _public_detail(value: Any) -> tuple[Any, int]:
    if isinstance(value, Mapping):
        public: dict[str, Any] = {}
        hidden_count = 0
        for key, child in value.items():
            name = str(key)
            if name.startswith("_") or _SENSITIVE_KEY_RE.search(name):
                hidden_count += 1
                continue
            public_child, child_hidden_count = _public_detail(child)
            public[name] = public_child
            hidden_count += child_hidden_count
        return public, hidden_count
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        public_items = []
        hidden_count = 0
        for child in value:
            public_child, child_hidden_count = _public_detail(child)
            public_items.append(public_child)
            hidden_count += child_hidden_count
        return public_items, hidden_count
    return ("-" if value in (None, "") else deepcopy(value)), 0


def _record_identities(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for identity_type, fields in _IDENTITY_FIELDS.items():
        for field in fields:
            candidate = value.get(field)
            if candidate not in (None, ""):
                identities.add((identity_type, str(candidate).strip().casefold()))
    return identities


def _item_key_identities(item_key: str) -> set[tuple[str, str]]:
    if item_key.startswith("ct:"):
        nct_id = item_key.split(":", 1)[1].strip()
        return {("nct", nct_id.casefold())} if nct_id else set()
    if item_key.startswith("patent:"):
        match = re.search(r"(?:^|:)(10-\d+)(?::|$)", item_key, re.IGNORECASE)
        return {("patent", match.group(1).casefold())} if match else set()
    return set()


def _related_contexts(
    trace: Mapping[str, Any], identities: set[tuple[str, str]]
) -> tuple[Mapping[str, Any], ...]:
    inspection = trace.get("inspection_detail")
    calls = inspection.get("calls") if isinstance(inspection, Mapping) else None
    inspection_calls = (
        [call for call in calls if isinstance(call, Mapping)]
        if isinstance(calls, Sequence) and not isinstance(calls, str | bytes)
        else []
    )
    tool_results = trace.get("tool_results")
    tools = (
        [result for result in tool_results if isinstance(result, Mapping)]
        if isinstance(tool_results, Sequence)
        and not isinstance(tool_results, str | bytes)
        else []
    )
    direct = [
        context
        for context in (*inspection_calls, *tools)
        if identities.intersection(_nested_identities(context))
    ]
    sources = {_context_source(context) for context in direct} - {""}
    related_calls = [
        call for call in inspection_calls if _context_source(call) in sources
    ]
    return (*related_calls, *direct)


def _nested_identities(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        identity
        for record in _walk_mappings(value)
        for identity in _record_identities(record)
    }


def _context_source(value: Mapping[str, Any]) -> str:
    source = (
        str(
            value.get("lane_id")
            or value.get("source")
            or value.get("lane")
            or value.get("tool")
            or ""
        )
        .strip()
        .casefold()
    )
    for canonical, aliases in (
        ("clinicaltrials", ("clinical", "ctgov")),
        ("patent", ("patent", "mfds")),
        ("document", ("document", "file_vdb", "file_rag")),
        ("hira", ("hira",)),
        ("mart", ("mart",)),
        ("web", ("web", "tavily")),
    ):
        if any(alias in source for alias in aliases):
            return canonical
    return source


def _first_context_value(
    contexts: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> Any:
    for context in contexts:
        nested = context.get("output")
        candidates = (context, nested) if isinstance(nested, Mapping) else (context,)
        for candidate_mapping in candidates:
            for key in keys:
                candidate = candidate_mapping.get(key)
                if candidate not in (None, "", {}, []):
                    return deepcopy(candidate)
    return None


def _detail_input(contexts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    request_parameters = _first_context_value(
        contexts, ("request_parameters", "parameters", "params")
    )
    if request_parameters is None:
        request_parameters = _nested_request_parameters(contexts)
    return {
        "query": _first_context_value(contexts, ("query", "search_query")) or "-",
        "request_parameters": request_parameters or {},
        "expansion_grade": _first_context_value(
            contexts, ("expansion_grade", "expansion_level")
        )
        or "-",
    }


def _query_breakdown(
    contexts: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    value = _first_context_value(contexts, ("by_query",))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [deepcopy(item) for item in value if isinstance(item, Mapping)]
    rows: list[Mapping[str, Any]] = []
    seen: set[tuple[str, int, int | None]] = set()
    for context in contexts:
        for record in _walk_mappings(context):
            render_data = record.get("render_data")
            if not isinstance(render_data, Mapping):
                continue
            request = render_data.get("request")
            request = request if isinstance(request, Mapping) else {}
            query = str(request.get("search") or request.get("query") or "").strip()
            payload = render_data.get("payload")
            sequences = _source_record_sequences(payload)
            received_count = max((len(items) for items in sequences), default=0)
            total_count = _nested_total_count(payload)
            if not query and not received_count and total_count is None:
                continue
            key = (query, received_count, total_count)
            if key in seen:
                continue
            seen.add(key)
            row: dict[str, Any] = {
                "query": query or "-",
                "received_count": received_count,
            }
            if total_count is not None:
                row["total_count"] = total_count
            rows.append(row)
    return rows


def _nested_request_parameters(
    contexts: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for context in contexts:
        for record in _walk_mappings(context):
            request = record.get("request")
            if isinstance(request, Mapping) and request:
                return deepcopy(dict(request))
    return None


def _nested_total_count(value: Any) -> int | None:
    for record in _walk_mappings(value):
        total = record.get("total")
        if isinstance(total, int) and total >= 0:
            return total
    return None


def _source_responses(
    contexts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for context in contexts:
        query = str(context.get("query") or context.get("search_query") or "-").strip()
        for record in _walk_mappings(context):
            content = record.get("content_text")
            if not isinstance(content, str) or not content:
                continue
            key = (query, content)
            if key in seen:
                continue
            seen.add(key)
            responses.append(
                {
                    "query": query or "-",
                    "content_text": content,
                    "length": len(content),
                }
            )
    return responses


def _internal_trace_field_paths(value: Any, path: str = "") -> list[str]:
    fields: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if name.casefold() in _INTERNAL_TRACE_FIELDS:
                fields.append(child_path)
            fields.extend(_internal_trace_field_paths(child, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, child in enumerate(value):
            fields.extend(_internal_trace_field_paths(child, f"{path}[{index}]"))
    return fields


def _detail_output(contexts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    elapsed_ms = _first_context_value(contexts, ("elapsed_ms",))
    if elapsed_ms is None:
        elapsed_seconds = _first_context_value(contexts, ("elapsed_seconds",))
        elapsed_ms = (
            round(float(elapsed_seconds) * 1000) if elapsed_seconds is not None else "-"
        )
    received_count = _first_context_value(
        contexts, ("received_count", "returned", "total_count")
    )
    if received_count is None:
        received_count = max(
            (
                len(records)
                for context in contexts
                for records in _source_record_sequences(context)
            ),
            default=0,
        )
    return {
        "received_count": received_count,
        "directly_relevant_count": _first_context_value(
            contexts, ("directly_relevant_count", "direct_related_count")
        )
        or 0,
        "summary": _first_context_value(contexts, ("summary", "response_summary"))
        or "-",
        "called_at": _first_context_value(
            contexts, ("called_at", "collected_at", "fetched_at")
        )
        or "-",
        "elapsed_ms": elapsed_ms,
    }


def _missing_fields(value: Any, path: str = "") -> dict[str, str]:
    missing: dict[str, str] = {}
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if child == "-":
                missing[child_path] = "원천 응답에 값이 없습니다."
            else:
                missing.update(_missing_fields(child, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, child in enumerate(value):
            missing.update(_missing_fields(child, f"{path}[{index}]"))
    return missing


def _length_hints(value: Any, path: str = "") -> dict[str, int]:
    hints: dict[str, int] = {}
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            hints.update(_length_hints(child, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, child in enumerate(value):
            hints.update(_length_hints(child, f"{path}[{index}]"))
    elif isinstance(value, str) and len(value) > 320:
        hints[path] = len(value)
    return hints


def _find_web_detail(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        source = str(value.get("source") or value.get("source_name") or "").casefold()
        if source in _WEB_SOURCES and value.get("url"):
            return value
        for child in value.values():
            found = _find_web_detail(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_web_detail(child)
            if found is not None:
                return found
    return None


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname.casefold() not in {"localhost"}
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


def _tavily_extract(url: str) -> str:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is unavailable")
    response = requests.post(
        "https://api.tavily.com/extract",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"urls": [url], "extract_depth": "advanced", "format": "text"},
        timeout=_WEB_EXTRACT_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    results = body.get("results") if isinstance(body, Mapping) else None
    if (
        not isinstance(results, Sequence)
        or isinstance(results, str | bytes)
        or not results
    ):
        return ""
    first = results[0]
    if not isinstance(first, Mapping):
        return ""
    return str(first.get("raw_content") or first.get("content") or "")


def _evidence_roots(trace: Mapping[str, Any]) -> tuple[Any, ...]:
    answer_sections = trace.get("answer_sections")
    section_catalog = (
        answer_sections.get("evidence_catalog")
        if isinstance(answer_sections, Mapping)
        else None
    )
    return (
        trace.get("inspection_detail"),
        trace.get("tool_results"),
        trace.get("evidence_catalog"),
        section_catalog,
    )


def _record_information_score(value: Any) -> tuple[int, int]:
    if isinstance(value, Mapping):
        child_scores = [_record_information_score(child) for child in value.values()]
        return (
            1 + sum(score[0] for score in child_scores),
            len(value) + sum(score[1] for score in child_scores),
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        child_scores = [_record_information_score(child) for child in value]
        return (
            1 + sum(score[0] for score in child_scores),
            len(value) + sum(score[1] for score in child_scores),
        )
    return (1, len(str(value)))


__all__ = [
    "DETAIL_OWNER_TRACE_KEY",
    "DETAIL_SCHEMA",
    "archive_trace_for_detail",
    "attach_detail_contract",
    "compact_inline_detail",
    "hydrate_web_detail",
    "resolve_detail_item",
]
