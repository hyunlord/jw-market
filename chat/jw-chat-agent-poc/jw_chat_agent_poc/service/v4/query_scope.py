from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import os
import re
from typing import Any, Literal


DEFAULT_ENTITY_LIMIT = 8
HARD_ENTITY_LIMIT = 12
SOURCE_CALL_LIMIT = 12

FailureReason = Literal[
    "RATE_LIMITED",
    "QUOTA_EXCEEDED",
    "AUTH_FAILED",
    "UPSTREAM_5XX",
    "NETWORK",
    "TIMEOUT",
    "UNKNOWN",
]


def configured_entity_limit() -> int:
    """Return the requested entity fan-out, bounded by the hard safety limit."""
    try:
        configured = int(os.environ.get("CHAT_V4_ENTITY_LIMIT", str(DEFAULT_ENTITY_LIMIT)))
    except ValueError:
        configured = DEFAULT_ENTITY_LIMIT
    return max(1, min(configured, HARD_ENTITY_LIMIT))

_AUTH_RE = re.compile(
    r"SERVICE_KEY_IS_NOT_REGISTERED_ERROR|SERVICE_KEY_IS_NOT_REGISTERED|"
    r"UNREGISTERED_SERVICE_KEY|INVALID(?:_|\s)*API(?:_|\s)*KEY|\b(?:401|403)\b",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR|QUOTA(?:_|\s)*EXCEEDED|"
    r"USAGE(?:_|\s)*LIMIT|CREDIT(?:S)?(?:_|\s)*(?:EXHAUSTED|DEPLETED)",
    re.IGNORECASE,
)
_RATE_RE = re.compile(r"\b429\b|RATE(?:_|\s)*LIMIT|TOO(?:_|\s)*MANY(?:_|\s)*REQUESTS", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"TIMED?\s*OUT|TIMEOUT", re.IGNORECASE)
_NETWORK_RE = re.compile(
    r"CONNECTION(?:_|\s)*(?:REFUSED|RESET|ABORTED)|DNS|NETWORK(?:_|\s)*UNREACHABLE",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|service[_-]?key|authorization|token|password)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_HIRA_CODE_YEAR_RE = re.compile(
    r"^(?P<code>[A-Z]\d{2}(?:\.?\d{1,2})?)\b.*?\b(?P<year>20\d{2})년\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def query_expansion_data() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "fixtures" / "v4_query_expansion.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != "r12.8.v1":
        raise ValueError("unsupported v4 query expansion schema")
    return value


def disease_kcd_codes(text: str) -> tuple[str, ...]:
    normalized = "".join(text.casefold().split())
    for disease, codes in query_expansion_data()["disease_kcd_sets"].items():
        if "".join(disease.casefold().split()) in normalized:
            return tuple(str(code).upper() for code in codes)
    return ()


def strip_mapped_disease_names(text: str) -> str:
    value = text
    for disease in query_expansion_data()["disease_kcd_sets"]:
        value = re.sub(re.escape(disease), " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def disease_brand_set(text: str) -> tuple[tuple[str, ...], str | None]:
    normalized = "".join(text.casefold().split())
    for disease, entry in query_expansion_data()["disease_brand_sets"].items():
        if "".join(disease.casefold().split()) in normalized:
            return tuple(str(brand) for brand in entry["brands"]), str(entry["source"])
    return (), None


def classify_upstream_failure(*, http_status: int | None, body: str) -> FailureReason:
    combined = f"{http_status or ''} {body}"
    if _AUTH_RE.search(combined):
        return "AUTH_FAILED"
    if _QUOTA_RE.search(combined):
        return "QUOTA_EXCEEDED"
    if http_status == 429 or _RATE_RE.search(combined):
        return "RATE_LIMITED"
    if http_status is not None and 500 <= http_status <= 599:
        return "UPSTREAM_5XX"
    if _TIMEOUT_RE.search(combined):
        return "TIMEOUT"
    if _NETWORK_RE.search(combined):
        return "NETWORK"
    return "UNKNOWN"


def redact_failure_body(body: str, *, limit: int = 2000) -> str:
    """Keep provider diagnostics while removing credential-shaped values."""
    return _SECRET_RE.sub(r"\1\2***", body)[:limit]


def _balanced_hira_queries(queries: tuple[str, ...]) -> tuple[str, ...]:
    parsed = [(_HIRA_CODE_YEAR_RE.search(query), query) for query in queries]
    if not parsed or any(match is None for match, _query in parsed):
        return queries
    codes = tuple(dict.fromkeys(match.group("code").upper() for match, _query in parsed if match))
    years = tuple(dict.fromkeys(match.group("year") for match, _query in parsed if match))
    if len(codes) < 2 or len(years) < 2:
        return queries
    by_axis = {
        (match.group("code").upper(), match.group("year")): query
        for match, query in parsed
        if match
    }
    balanced = tuple(
        by_axis[(code, year)]
        for year in years
        for code in codes
        if (code, year) in by_axis
    )
    return balanced if len(balanced) == len(queries) else queries


def apply_source_call_cap(plan: Any) -> Any:
    """Apply the post-expansion call cap while preserving CT's lossless contract."""
    from jw_chat_agent_poc.service.v4.contracts import QueryScope

    updates: dict[str, tuple[str, ...]] = {}
    requested: dict[str, int] = {}
    executed: dict[str, int] = {}
    omitted: dict[str, tuple[str, ...]] = {}
    previous = getattr(plan, "query_scope", None)
    for source, queries in plan.tool_queries.items():
        unique = tuple(dict.fromkeys(queries))
        selection_order = _balanced_hira_queries(unique) if source == "hira" else unique
        selected = (
            unique
            if source == "clinicaltrials"
            else selection_order[:SOURCE_CALL_LIMIT]
        )
        updates[source] = selected
        previous_omitted = (
            tuple(previous.omitted_queries.get(source, ())) if previous is not None else ()
        )
        preserve_first_wave_scope = bool(previous_omitted) and len(unique) <= SOURCE_CALL_LIMIT
        requested[source] = (
            max(int(previous.requested_calls.get(source, 0)), len(unique))
            if preserve_first_wave_scope
            else len(unique)
        )
        executed[source] = len(selected)
        if preserve_first_wave_scope:
            omitted[source] = previous_omitted
        elif len(selected) < len(unique):
            omitted[source] = selection_order[len(selected) :]
    return plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(update=updates),
            "query_scope": QueryScope(
                requested_calls=requested,
                executed_calls=executed,
                omitted_queries=omitted,
            ),
        }
    )
