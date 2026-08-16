from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
import json
import logging
import re
import threading
from typing import Any, Literal, Protocol

import requests


FailureClass = Literal["timeout", "5xx", "0_results", "schema", "quota", "none"]
DomainSource = Literal["cache", "MCP", "web"]
CacheObservation = Literal["hit", "stale", "miss", "not_applicable"]

_LOGGER = logging.getLogger("uvicorn.error")
_EVALUATOR_EXCEPTION_COUNTS: Counter[str] = Counter()
_EVALUATOR_EXCEPTION_LOCK = threading.Lock()
_HTTP_5XX_RE = re.compile(r"(?<!\d)5\d{2}(?!\d)")


class _ExternalCallLike(Protocol):
    status: str
    render_data: Mapping[str, Any]


def question_fingerprint(question: str) -> str:
    normalized = " ".join(question.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def failure_class_from_exception(exc: BaseException) -> FailureClass:
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        status_code = getattr(exc.response, "status_code", None)
        if status_code == 429:
            return "quota"
        if isinstance(status_code, int) and 500 <= status_code <= 599:
            return "5xx"
    text = str(exc).casefold()
    if any(
        token in text
        for token in (
            "quota",
            "usage limit",
            "plan's set usage limit",
            "rate limit",
            "too many requests",
            "limited_number_of_service_requests_exceeds_error",
        )
    ):
        return "quota"
    if any(token in text for token in ("timeout", "timed out", "deadline exceeded")):
        return "timeout"
    if _HTTP_5XX_RE.search(text):
        return "5xx"
    if any(token in text for token in ("schema", "json", "decode", "validation")):
        return "schema"
    return "none"


def failure_class_from_call(call: _ExternalCallLike) -> FailureClass:
    if call.status == "no_data":
        return "0_results"
    if call.status != "error":
        return "none"
    if str(call.render_data.get("error_type") or "").casefold() == "quota":
        return "quota"
    error = call.render_data.get("error")
    return failure_class_from_exception(RuntimeError(str(error or "")))


def emit_external_call_telemetry(
    *,
    primary_provider: str,
    question: str,
    domain_source: DomainSource,
    cache_status: CacheObservation,
    call: _ExternalCallLike,
    fallback_blocked: bool = False,
) -> None:
    try:
        failure_class = failure_class_from_call(call)
    except Exception as exc:
        _record_evaluator_exception(exc, primary_provider=primary_provider)
        return
    emit_external_source_telemetry(
        primary_provider=primary_provider,
        question=question,
        failure_class=failure_class,
        domain_source=domain_source,
        cache_status=cache_status,
        fallback_blocked=fallback_blocked,
    )


def emit_external_source_telemetry(
    *,
    primary_provider: str,
    question: str,
    failure_class: FailureClass,
    domain_source: DomainSource,
    cache_status: CacheObservation,
    fallback_blocked: bool = False,
) -> None:
    payload = {
        "primary_provider": primary_provider,
        "failure_class": failure_class,
        "domain_source": domain_source,
        "cache_status": cache_status,
        "fallback_eligible": (
            not fallback_blocked
            and failure_class in {"timeout", "5xx", "0_results"}
        ),
        "question_fingerprint": question_fingerprint(question),
    }
    _LOGGER.info(
        "external_source_telemetry %s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def evaluator_exception_counts() -> dict[str, int]:
    with _EVALUATOR_EXCEPTION_LOCK:
        return dict(_EVALUATOR_EXCEPTION_COUNTS)


def reset_evaluator_exception_counts() -> None:
    with _EVALUATOR_EXCEPTION_LOCK:
        _EVALUATOR_EXCEPTION_COUNTS.clear()


def _record_evaluator_exception(exc: Exception, *, primary_provider: str) -> None:
    error_type = type(exc).__name__
    with _EVALUATOR_EXCEPTION_LOCK:
        _EVALUATOR_EXCEPTION_COUNTS[error_type] += 1
        count = _EVALUATOR_EXCEPTION_COUNTS[error_type]
    _LOGGER.exception(
        "external_source_telemetry_evaluator_failed provider=%s error_type=%s count=%d",
        primary_provider,
        error_type,
        count,
    )
