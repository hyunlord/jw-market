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
    calls = []
    for index, result in enumerate(results, start=1):
        evidence = sets_by_source.get(result.source)
        source_ids = {
            record.evidence_id for record in evidence.records
        } if evidence is not None else set()
        returned = _returned_count(result.payload) if result.status == "ok" else 0
        parsed = len(source_ids)
        rendered_count = len(source_ids & rendered_ids)
        narrated = len(source_ids & narrated_ids)
        calls.append(
            {
                "sequence": index,
                "source_label": public_source_label(result.source),
                "status": _public_status(result, returned),
                "elapsed_seconds": round(max(result.elapsed_ms, 0.0) / 1000, 3),
                "request_parameters": {"query": _sanitize(result.query)},
                "counts": {
                    "returned": returned,
                    "parsed": parsed,
                    "rendered": rendered_count,
                    "narrated": narrated,
                },
                "unused_count": max(returned - narrated, 0),
                "dropped_count": max(returned - parsed, 0),
                "drop_reasons": _drop_reasons(result, returned, parsed, rendered_count, narrated),
            }
        )
    return {
        "schema": "r12.5.inspect.v1",
        "question": _sanitize(plan.resolved_question),
        "expansion": _sanitize_value(dict(expansion or {})),
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
    returned: int,
    parsed: int,
    rendered: int,
    narrated: int,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if returned > parsed:
        reasons.append({"stage": "parsing", "count": returned - parsed, "reason": "검증 가능한 레코드로 변환되지 않음"})
    if parsed > rendered:
        reasons.append({"stage": "render", "count": parsed - rendered, "reason": "현재 답변 표면에 배치되지 않음"})
    if rendered > narrated:
        reasons.append({"stage": "narrative", "count": rendered - narrated, "reason": "표에는 있으나 서술에 직접 등장하지 않음"})
    if result.status != "ok" and result.notice:
        reasons.append({"stage": "retrieval", "count": 0, "reason": _sanitize(result.notice)})
    return reasons


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
