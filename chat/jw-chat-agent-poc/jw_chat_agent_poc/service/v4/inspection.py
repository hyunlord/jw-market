from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any
from urllib.parse import urlparse

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import DeterministicRender, EvidenceSet
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|authorization|secret|password)\s*[:=]\s*[^\s,&]+"
)


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
        calls.append(
            {
                "sequence": index,
                "source_label": public_source_label(result.source),
                "status": _public_status(result, returned),
                "elapsed_seconds": round(max(result.elapsed_ms, 0.0) / 1000, 3),
                "request_parameters": _request_parameters(result),
                "output": _inspection_output(raw_records, returned),
                "counts": {
                    "returned": returned,
                    "parsed": parsed,
                    "envelope": envelope,
                    "rendered": rendered_count,
                    "narrated": narrated,
                },
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
    }


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
                    records.extend(nested)
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
        "patent_number",
        "application_number",
        "item_name",
        "item_seq",
        "product_name",
        "brand",
        "sickCd",
        "sickNm",
        "title",
        "brief_title",
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
) -> dict[str, Any]:
    return {
        "returned": returned,
        "records": [
            {"identifiers": sorted(_display_identifiers(record)) or ["식별자 없음"]}
            for record in records
        ],
    }


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
