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
    "scope_limit",
]
FailureExposure = Literal["F-actionable", "F-scope", "F-internal"]

_TIMEOUT_RE = re.compile(r"(?:timed?\s*out|timeout|시간\s*초과|응답\s*지연)", re.IGNORECASE)
_QUOTA_RE = re.compile(
    r"(?:\b429\b|too[_\s-]*many[_\s-]*requests|rate[_\s-]*limit|quota|"
    r"usage\s*limit|plan.+limit|사용량\s*한도)",
    re.IGNORECASE,
)
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
    exposure_layer: FailureExposure
    public_notice: str | None = None


def classify_retrieval_status(result: SourceResult) -> RetrievalStatus:
    failure_reason = str(result.failure_reason or "")
    if failure_reason in {"RATE_LIMITED", "QUOTA_EXCEEDED"}:
        return "quota"
    if failure_reason == "TIMEOUT":
        return "timeout"
    if failure_reason in {"AUTH_FAILED", "UPSTREAM_5XX", "NETWORK"}:
        return "upstream"
    notice = str(result.notice or "")
    if result.status == "empty" and notice:
        signaled = classify_failure_signals((result.status,), notice)
        if signaled != "empty":
            return signaled
    if result.status in {
        "ok",
        "empty",
        "timeout",
        "quota",
        "upstream",
        "parse_error",
        "deadline_exceeded",
        "scope_limit",
    }:
        return result.status
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
    if _QUOTA_RE.search(notice) or any(
        status in {"429", "too_many_requests", "rate_limit", "quota"}
        or "429" in status
        for status in normalized
    ):
        return "quota"
    if _TIMEOUT_RE.search(notice) or "timeout" in normalized:
        return "timeout"
    if _PARSE_RE.search(notice) or "parse_error" in normalized:
        return "parse_error"
    if _UPSTREAM_RE.search(notice) or any(
        status in {"error", "missing_key"} for status in normalized
    ):
        return "upstream"
    if "scope_limit" in normalized:
        return "scope_limit"
    if normalized and all(status in {"no_data", "empty"} for status in normalized):
        return "empty"
    return "upstream"


def failure_status_from_value(value: Any) -> RetrievalStatus | None:
    status = str(value or "").strip().casefold()
    if not status:
        return None
    if (
        status in {"429", "http_429", "too_many_requests", "rate_limit", "quota"}
        or "429" in status
        or _QUOTA_RE.search(status)
    ):
        return "quota"
    if status == "deadline_exceeded":
        return "deadline_exceeded"
    if status == "timeout" or _TIMEOUT_RE.search(status):
        return "timeout"
    if status == "parse_error" or _PARSE_RE.search(status):
        return "parse_error"
    if status in {"error", "missing_key", "unsupported", "upstream"} or _UPSTREAM_RE.search(
        status
    ):
        return "upstream"
    if status in {"empty", "no_data"}:
        return "empty"
    if status == "scope_limit":
        return "scope_limit"
    return None


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
    reason_code = str(result.failure_reason or status)
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
        received_count=_received_count(result.payload) if status == "ok" else 0,
        reason_code=reason_code,
        exposure_layer=_exposure_layer(status),
        public_notice=(
            str(result.notice).strip()
            if status == "scope_limit" and str(result.notice or "").strip()
            else None
        ),
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
        if event.tool == "web":
            return (
                f"{prefix or '웹 검색 '}사용량 한도 초과로 "
                "외부 조회가 실패해 확인할 수 없습니다."
            )
        if event.tool in {"nedrug", "patent"}:
            return "식품의약품안전처 조회 한도 초과로 확인할 수 없습니다."
        return (
            f"{prefix}제공자 사용량 한도 초과로 외부 조회가 실패해 "
            "확인할 수 없습니다."
        )
    if event.status == "upstream":
        if event.reason_code == "AUTH_FAILED":
            return f"{prefix}외부 조회 인증에 실패해 확인할 수 없습니다."
        if event.reason_code == "UPSTREAM_5XX":
            return f"{prefix}상류 서비스 오류로 조회에 실패해 확인할 수 없습니다."
        if event.reason_code == "NETWORK":
            return f"{prefix}외부 서비스 연결에 실패해 확인할 수 없습니다."
        return f"{prefix}외부 조회가 실패해 확인할 수 없습니다."
    if event.status == "parse_error":
        return f"{prefix}응답은 받았으나 검증 가능한 레코드로 변환하지 못했습니다."
    if event.status == "scope_limit":
        if event.public_notice:
            notice = event.public_notice.strip()
            if notice and notice[-1] not in ".?!。？！":
                notice = f"{notice}."
            return f"{prefix}{notice}"
        return (
            f"{prefix}성분명으로는 품목 검색이 지원되지 않아 "
            "이 항목은 확인하지 못했습니다."
        )
    return f"{prefix}조회가 완료되었습니다."


def _exposure_layer(status: RetrievalStatus) -> FailureExposure:
    if status == "scope_limit":
        return "F-scope"
    if status in {"timeout", "quota", "upstream", "parse_error", "deadline_exceeded"}:
        return "F-actionable"
    return "F-internal"


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
    if not status or failure_status_from_value(status) is not None:
        return False
    render_data = call.get("render_data")
    nested_status = render_data.get("status") if isinstance(render_data, Mapping) else None
    return failure_status_from_value(nested_status) is None
