from __future__ import annotations


_URL_PAYLOAD_KEYS = frozenset(
    {"url", "source_url", "record_url", "safe_url", "href", "link"}
)
_REQUEST_METADATA_KEYS = frozenset(
    {
        "compiled_expression",
        "endpoint",
        "filter",
        "filters",
        "matched_query",
        "parameters",
        "params",
        "query",
        "queries",
        "request",
        "request_url",
        "source_query",
        "source_queries",
        *_URL_PAYLOAD_KEYS,
    }
)


def is_request_metadata_key(value: str, *, include_url_fields: bool = True) -> bool:
    normalized = value.strip().casefold()
    if not include_url_fields and normalized in _URL_PAYLOAD_KEYS:
        return False
    return (
        normalized in _REQUEST_METADATA_KEYS
        or normalized.startswith(("query.", "filter."))
        or normalized.endswith(("_query", "_queries", "_request", "_parameters"))
    )


def is_url_payload_key(value: str) -> bool:
    return value.strip().casefold() in _URL_PAYLOAD_KEYS
