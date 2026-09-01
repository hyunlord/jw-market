from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

from jw_chat_agent_poc.service.detail_on_demand import (
    attach_detail_contract,
    compact_inline_detail,
)
from jw_chat_agent_poc.service.sse_presenter import normalize_structured_answer_trace

LOGGER = logging.getLogger(__name__)

SHADOW_TRANSPORT_ENV: Final = "CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED"
INLINE_DETAIL_ENV: Final = "JW_CHAT_INLINE_DETAIL_ENABLED"
TRANSPORT_BUDGET_BYTES: Final = 15 * 1024 * 1024
_TRUE_VALUES: Final = frozenset({"1", "true", "on", "enabled", "yes"})
_ARCHIVED_TRACE_FIELDS: Final = (
    "lossless_spine",
    "scope_provenance_projection",
    "claim_ir_realization",
    "_detail_owner_id",
)
_INSPECTION_SUMMARY_FIELDS: Final = (
    "returned",
    "displayed_record_count",
    "duplicate_records_collapsed",
    "evidence_refs",
)
_FILE_DETAIL_LANES: Final = frozenset({"file_vdb", "file_sql"})
_RECORD_COLLECTION_KEYS: Final = frozenset({"items", "records", "results", "rows", "studies"})
_TRUNCATION_NOTICE = (
    "응답 크기 한도로 전체 {received}건 중 {shown}건을 표시했습니다. "
    "나머지는 조회 상세에서 확인할 수 있습니다."
)
_UNRESOLVED_LIMIT_NOTICE = (
    "응답 크기 한도로 일부 내용을 표시하지 못했습니다. "
    "전체 기록은 조회 상세의 저장 기록에서 확인할 수 있습니다."
)


@dataclass(frozen=True, slots=True)
class TransportAnswer:
    text: str
    trace: dict[str, Any]
    truncated: bool
    transport_bytes: int


@dataclass(frozen=True, slots=True)
class _RecordCollection:
    path: tuple[str | int, ...]
    lane: str
    records: tuple[Any, ...]


def trace_for_transport(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Project a trace for clients without mutating the server-owned audit trace."""
    return project_answer_for_transport("", trace).trace


def project_answer_for_transport(
    text: str,
    trace: Mapping[str, Any],
    *,
    budget_bytes: int = TRANSPORT_BUDGET_BYTES,
) -> TransportAnswer:
    if budget_bytes < 2:
        raise ValueError("budget_bytes must fit a JSON object")

    excluded_fields = set(_ARCHIVED_TRACE_FIELDS)
    if os.getenv(SHADOW_TRANSPORT_ENV, "false").strip().casefold() not in _TRUE_VALUES:
        excluded_fields.add("claim_ir_shadow")
    archived_fields = {
        key: _json_bytes(value)
        for key, value in trace.items()
        if key in excluded_fields
    }
    projected = {
        key: deepcopy(value)
        for key, value in trace.items()
        if key not in excluded_fields
    }
    projected = normalize_structured_answer_trace(text, projected)
    inline_detail_enabled = (
        os.getenv(INLINE_DETAIL_ENV, "false").strip().casefold() in _TRUE_VALUES
    )
    projected = (
        attach_detail_contract(projected)
        if inline_detail_enabled
        else compact_inline_detail(projected)
    )
    inspection_compacted = (
        _compact_inspection_outputs(projected) if inline_detail_enabled else False
    )

    if not archived_fields and not inspection_compacted and not projected.get("tool_results"):
        transport_bytes = _json_bytes(projected)
        if transport_bytes <= budget_bytes:
            LOGGER.info(
                "chat_response_transport total_bytes=%d budget_bytes=%d remaining_bytes=%d "
                "truncated=false received_count=0 shown_count=0 truncated_count=0 "
                "component_bytes={} lane_counts={}",
                transport_bytes,
                budget_bytes,
                budget_bytes - transport_bytes,
            )
            return TransportAnswer(
                text=text,
                trace=projected,
                truncated=False,
                transport_bytes=transport_bytes,
            )

    collections = _record_collections(projected)
    received_by_lane = _lane_counts(collections, ratio=1.0)
    projected = _with_response_size(
        projected,
        archived_fields=archived_fields,
        received_by_lane=received_by_lane,
        shown_by_lane=received_by_lane,
        selection_method=_selection_method(projected),
        budget_bytes=budget_bytes,
    )

    truncated = False
    if _json_bytes(projected) > budget_bytes and collections:
        projected = _largest_fitting_projection(
            projected,
            collections,
            archived_fields=archived_fields,
            received_by_lane=received_by_lane,
            selection_method=_selection_method(projected),
            budget_bytes=budget_bytes,
        )
        truncated = sum(_response_lane_shown(projected).values()) < sum(received_by_lane.values())

    transport_bytes = _json_bytes(projected)
    budget_exceeded = transport_bytes > budget_bytes
    if budget_exceeded:
        projected = _reference_only_projection(budget_bytes=budget_bytes)
        transport_bytes = _json_bytes(projected)
    response_size = projected.get("response_size")
    shown_count = 0
    truncated_count = 0
    if isinstance(response_size, Mapping):
        shown_count = int(response_size.get("shown_count") or 0)
        truncated_count = int(response_size.get("truncated_count") or 0)
    output_text = text
    if budget_exceeded:
        output_text = (
            f"{text.rstrip()}\n\n> {_UNRESOLVED_LIMIT_NOTICE}"
            if text.strip()
            else _UNRESOLVED_LIMIT_NOTICE
        )
        truncated = True
    elif truncated:
        notice = _TRUNCATION_NOTICE.format(
            received=sum(received_by_lane.values()),
            shown=shown_count,
        )
        output_text = f"{text.rstrip()}\n\n> {notice}" if text.strip() else notice

    component_bytes = {
        key: _json_bytes(value)
        for key, value in projected.items()
        if key != "tool_results"
    }
    tool_results = projected.get("tool_results")
    component_bytes["tool_results"] = _json_bytes(tool_results if isinstance(tool_results, list) else [])
    LOGGER.info(
        "chat_response_transport total_bytes=%d budget_bytes=%d remaining_bytes=%d "
        "truncated=%s received_count=%d shown_count=%d truncated_count=%d "
        "component_bytes=%s lane_counts=%s",
        transport_bytes,
        budget_bytes,
        budget_bytes - transport_bytes,
        str(truncated).lower(),
        sum(received_by_lane.values()),
        shown_count,
        truncated_count,
        json.dumps(component_bytes, sort_keys=True, separators=(",", ":")),
        json.dumps(response_size.get("lanes", {}) if isinstance(response_size, Mapping) else {}, sort_keys=True, separators=(",", ":")),
    )
    return TransportAnswer(
        text=output_text,
        trace=projected,
        truncated=truncated,
        transport_bytes=transport_bytes,
    )


def _compact_inspection_outputs(trace: MutableMapping[str, Any]) -> bool:
    tool_results = trace.get("tool_results")
    sequences = {
        int(result["sequence"])
        for result in tool_results
        if isinstance(result, Mapping)
        and isinstance(result.get("sequence"), int)
        and "payload" in result
    } if isinstance(tool_results, list) else set()
    inspection = trace.get("inspection_detail")
    if not isinstance(inspection, MutableMapping):
        return False
    calls = inspection.get("calls")
    if not isinstance(calls, list):
        return False
    compacted = False
    for call in calls:
        if not isinstance(call, MutableMapping):
            continue
        if call.get("lane_id") in _FILE_DETAIL_LANES:
            continue
        sequence = call.get("trace_sequence")
        output = call.get("output")
        if not isinstance(sequence, int) or sequence not in sequences or not isinstance(output, Mapping):
            continue
        summary = {key: deepcopy(output[key]) for key in _INSPECTION_SUMMARY_FIELDS if key in output}
        summary["record_reference"] = {
            "retained_in": "tool_results",
            "trace_sequence": sequence,
        }
        call["output"] = summary
        compacted = True
    return compacted


def _record_collections(trace: Mapping[str, Any]) -> tuple[_RecordCollection, ...]:
    tool_results = trace.get("tool_results")
    if not isinstance(tool_results, list):
        return ()
    collections: list[_RecordCollection] = []
    for index, result in enumerate(tool_results):
        if not isinstance(result, Mapping):
            continue
        lane = str(result.get("source") or "unknown")
        _find_record_collections(
            result.get("payload"),
            path=("tool_results", index, "payload"),
            lane=lane,
            output=collections,
        )
    return tuple(collections)


def _find_record_collections(
    value: Any,
    *,
    path: tuple[str | int, ...],
    lane: str,
    output: list[_RecordCollection],
) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            nested_path = (*path, str(key))
            if (
                key in _RECORD_COLLECTION_KEYS
                and isinstance(nested, list)
                and nested
                and all(isinstance(item, Mapping) for item in nested)
            ):
                output.append(_RecordCollection(nested_path, lane, tuple(nested)))
                continue
            _find_record_collections(nested, path=nested_path, lane=lane, output=output)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _find_record_collections(nested, path=(*path, index), lane=lane, output=output)


def _largest_fitting_projection(
    trace: dict[str, Any],
    collections: Sequence[_RecordCollection],
    *,
    archived_fields: Mapping[str, int],
    received_by_lane: Mapping[str, int],
    selection_method: str,
    budget_bytes: int,
) -> dict[str, Any]:
    candidate = trace
    low = 0.0
    high = 1.0
    _project_at_ratio_in_place(
        candidate,
        collections,
        ratio=0.0,
        archived_fields=archived_fields,
        received_by_lane=received_by_lane,
        selection_method=selection_method,
        budget_bytes=budget_bytes,
    )
    if _json_bytes(candidate) > budget_bytes:
        return candidate
    for _ in range(12):
        ratio = (low + high) / 2
        _project_at_ratio_in_place(
            candidate,
            collections,
            ratio=ratio,
            archived_fields=archived_fields,
            received_by_lane=received_by_lane,
            selection_method=selection_method,
            budget_bytes=budget_bytes,
        )
        if _json_bytes(candidate) <= budget_bytes:
            low = ratio
        else:
            high = ratio
    _project_at_ratio_in_place(
        candidate,
        collections,
        ratio=low,
        archived_fields=archived_fields,
        received_by_lane=received_by_lane,
        selection_method=selection_method,
        budget_bytes=budget_bytes,
    )
    return candidate


def _project_at_ratio_in_place(
    trace: dict[str, Any],
    collections: Sequence[_RecordCollection],
    *,
    ratio: float,
    archived_fields: Mapping[str, int],
    received_by_lane: Mapping[str, int],
    selection_method: str,
    budget_bytes: int,
) -> None:
    for collection in collections:
        count = len(collection.records)
        keep = min(count, max(1, math.floor(count * ratio))) if count else 0
        _set_path(trace, collection.path, list(collection.records[:keep]))
    shown_by_lane = _lane_counts(collections, ratio=ratio)
    _with_response_size(
        trace,
        archived_fields=archived_fields,
        received_by_lane=received_by_lane,
        shown_by_lane=shown_by_lane,
        selection_method=selection_method,
        budget_bytes=budget_bytes,
    )


def _set_path(root: Any, path: Sequence[str | int], value: Any) -> None:
    cursor = root
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value


def _lane_counts(
    collections: Sequence[_RecordCollection],
    *,
    ratio: float,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for collection in collections:
        total = len(collection.records)
        shown = min(total, max(1, math.floor(total * ratio))) if total else 0
        counts[collection.lane] = counts.get(collection.lane, 0) + shown
    return dict(sorted(counts.items()))


def _with_response_size(
    trace: dict[str, Any],
    *,
    archived_fields: Mapping[str, int],
    received_by_lane: Mapping[str, int],
    shown_by_lane: Mapping[str, int],
    selection_method: str,
    budget_bytes: int,
) -> dict[str, Any]:
    received = sum(received_by_lane.values())
    shown = sum(shown_by_lane.values())
    trace["response_size"] = {
        "source_total": received,
        "received_count": received,
        "shown_count": shown,
        "truncated_count": max(received - shown, 0),
        "selection_method": selection_method,
        "archive_reference": "conversation_trace_json",
        "budget_bytes": budget_bytes,
        "archived_field_count": len(archived_fields),
        "archived_projection_bytes": sum(archived_fields.values()),
        "lanes": {
            lane: {
                "received_count": count,
                "shown_count": shown_by_lane.get(lane, 0),
                "truncated_count": max(count - shown_by_lane.get(lane, 0), 0),
            }
            for lane, count in sorted(received_by_lane.items())
        },
    }
    return trace


def _reference_only_projection(*, budget_bytes: int) -> dict[str, Any]:
    reference = {
        "response_size": {
            "archive_reference": "conversation_trace_json",
            "budget_bytes": budget_bytes,
            "budget_exceeded": True,
            "notice": _UNRESOLVED_LIMIT_NOTICE,
            "selection_method": "stored_record_reference",
        }
    }
    return reference if _json_bytes(reference) <= budget_bytes else {}


def _response_lane_shown(trace: Mapping[str, Any]) -> dict[str, int]:
    response_size = trace.get("response_size")
    lanes = response_size.get("lanes") if isinstance(response_size, Mapping) else None
    if not isinstance(lanes, Mapping):
        return {}
    return {
        str(lane): int(value.get("shown_count") or 0)
        for lane, value in lanes.items()
        if isinstance(value, Mapping)
    }


def _selection_method(trace: Mapping[str, Any]) -> str:
    if trace.get("selection_is_ranked") is True:
        return "leading_records_in_ranked_order"
    rule = trace.get("selection_rule")
    return str(rule) if isinstance(rule, str) and rule else "leading_records_in_upstream_order"


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


__all__ = [
    "SHADOW_TRANSPORT_ENV",
    "TRANSPORT_BUDGET_BYTES",
    "TransportAnswer",
    "project_answer_for_transport",
    "trace_for_transport",
]
