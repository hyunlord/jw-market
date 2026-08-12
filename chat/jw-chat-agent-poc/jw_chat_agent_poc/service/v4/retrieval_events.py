from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jw_chat_agent_poc.service.v4.contracts import SourceResult


RetrievalStatus = Literal[
    "ok",
    "empty",
    "timeout",
    "quota",
    "upstream",
    "parse_error",
    "deadline_exceeded",
]

_TIMEOUT_RE = re.compile(r"(?:timed?\s*out|timeout|시간\s*초과|응답\s*지연)", re.IGNORECASE)
_QUOTA_RE = re.compile(r"(?:quota|usage\s*limit|plan.+limit|사용량\s*한도)", re.IGNORECASE)
_PARSE_RE = re.compile(r"(?:parse|malformed|decode|schema|변환하지\s*못)", re.IGNORECASE)
_UPSTREAM_RE = re.compile(r"(?:HTTP\s*5\d\d|connection|upstream|503|502|504)", re.IGNORECASE)


class RetrievalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(pattern=r"^EXEC-")
    record_type: Literal["RetrievalEvent"] = "RetrievalEvent"
    tool: str
    entity_id: str | None = None
    status: RetrievalStatus
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    received_count: int = 0
    reason_code: str


def classify_retrieval_status(result: SourceResult) -> RetrievalStatus:
    if result.status in {
        "ok",
        "empty",
        "timeout",
        "quota",
        "upstream",
        "parse_error",
        "deadline_exceeded",
    }:
        return result.status
    notice = str(result.notice or "")
    if _QUOTA_RE.search(notice):
        return "quota"
    if _TIMEOUT_RE.search(notice):
        return "timeout"
    if _PARSE_RE.search(notice):
        return "parse_error"
    return "upstream"


def classify_failure_signals(
    statuses: tuple[str, ...],
    notice: str,
) -> RetrievalStatus:
    normalized = tuple(status.casefold() for status in statuses if status)
    if _QUOTA_RE.search(notice):
        return "quota"
    if _TIMEOUT_RE.search(notice) or "timeout" in normalized:
        return "timeout"
    if _PARSE_RE.search(notice) or "parse_error" in normalized:
        return "parse_error"
    if _UPSTREAM_RE.search(notice) or any(
        status in {"error", "missing_key"} for status in normalized
    ):
        return "upstream"
    if normalized and all(status in {"no_data", "empty"} for status in normalized):
        return "empty"
    return "upstream"


def retrieval_event_from_result(
    result: SourceResult,
    *,
    entity_id: str | None = None,
    completed_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> RetrievalEvent:
    completed = completed_at
    started = (
        completed - timedelta(milliseconds=max(result.elapsed_ms, 0.0))
        if completed is not None
        else None
    )
    status = classify_retrieval_status(result)
    stable_input = "\x1f".join(
        (
            result.source,
            result.query,
            entity_id or "",
            status,
            str(result.notice or ""),
        )
    )
    return RetrievalEvent(
        record_id="EXEC-" + sha256(stable_input.encode("utf-8")).hexdigest()[:20],
        tool=result.source,
        entity_id=entity_id,
        status=status,
        started_at=started,
        deadline_at=deadline_at,
        completed_at=completed,
        received_count=_received_count(result.payload),
        reason_code=status,
    )


def public_retrieval_notice(
    event: RetrievalEvent,
    *,
    label: str | None = None,
) -> str:
    prefix = f"{label} " if label else ""
    if event.status == "empty":
        return f"{prefix}이번 조회 조건에 맞는 레코드 0건으로 확인되었습니다."
    if event.status in {"timeout", "deadline_exceeded"}:
        return (
            f"{prefix}응답 시간 내 도착하지 않아 이번 답변에서 제외되었으며, "
            "조회가 완료되지 않아 확인할 수 없습니다."
        )
    if event.status == "quota":
        return (
            f"{prefix}제공자 사용량 한도 초과로 외부 조회가 실패해 "
            "확인할 수 없습니다."
        )
    if event.status == "upstream":
        return f"{prefix}외부 조회가 실패해 확인할 수 없습니다."
    if event.status == "parse_error":
        return f"{prefix}응답은 받았으나 검증 가능한 레코드로 변환하지 못했습니다."
    return f"{prefix}조회가 완료되었습니다."


def utc_now() -> datetime:
    return datetime.now(UTC)


def _received_count(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    for key in ("records", "rows", "items", "studies"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    calls = payload.get("calls")
    if isinstance(calls, list):
        return sum(1 for call in calls if isinstance(call, Mapping) and _call_has_data(call))
    nested = payload.get("payload")
    return _received_count(nested) if isinstance(nested, Mapping) else 0


def _call_has_data(call: Mapping[str, Any]) -> bool:
    status = str(call.get("status") or "").casefold()
    return status not in {"", "error", "no_data", "unsupported", "timeout"}
