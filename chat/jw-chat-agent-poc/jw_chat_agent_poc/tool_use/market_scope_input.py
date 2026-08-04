from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    AmbiguousFamilyError,
    InvalidMarketLabelError,
    NoAnchorError,
    UnknownBrandError,
)


ATC4_PATTERN = re.compile(r"[A-Z]\d{1,2}[A-Z]\d?", re.IGNORECASE)
STRATEGIC_MARKET_PATTERN = re.compile(r"ml_\d+", re.IGNORECASE)
SUPPORTED_SOURCES = frozenset({"", "ubist", "iqvia"})
_VIEW_LABELS = {
    "전략뷰": "strategic",
    "strategic": "strategic",
    "strategic_ml": "strategic",
    "market_landscape": "strategic",
    "일반뷰": "general",
    "general": "general",
    "general_view": "general",
}


def normalize_view_arguments(
    arguments: Mapping[str, object],
) -> tuple[dict[str, object], tuple[str, ...]]:
    normalized = dict(arguments)
    notes: list[str] = []
    requested_view = optional_text(normalized, "view")
    for key in ("market", "source"):
        value = optional_text(normalized, key)
        view = _VIEW_LABELS.get(value.casefold()) if value else None
        if view is None:
            continue
        if requested_view is not None and requested_view != view:
            raise InvalidMarketLabelError(
                f"conflicting view labels: {requested_view}, {value}"
            )
        normalized[key] = None if key == "market" else ""
        normalized["view"] = view
        requested_view = view
        notes.append(f"{key}:{value}->view:{view}")
    if requested_view is not None:
        normalized["view"] = _VIEW_LABELS.get(
            requested_view.casefold(), requested_view.casefold()
        )
    return normalized, tuple(notes)


def normalize_source(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    return "iqvia" if normalized in {"iqvia", "iqvia_nsa", "nsa"} else normalized


def scope_filters(raw: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(raw, Mapping):
        return ()
    filters: list[tuple[str, tuple[str, ...]]] = []
    for key, value in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise InvalidMarketLabelError(f"scope filter {key} must be a list")
        filters.append((str(key), tuple(str(item) for item in value)))
    return tuple(filters)


def raise_unresolved_brand(brand: str) -> None:
    compact = "".join(brand.split())
    if compact.endswith("패밀리"):
        raise AmbiguousFamilyError(brand)
    if compact in {"이시장", "그시장", "해당시장"}:
        raise NoAnchorError(brand)
    raise UnknownBrandError(brand)


def required_text(arguments: Mapping[str, object], key: str) -> str:
    value = optional_text(arguments, key)
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def optional_text(arguments: Mapping[str, object], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def text(arguments: Mapping[str, object], key: str, default: str) -> str:
    return optional_text(arguments, key) or default


def integer(arguments: Mapping[str, object], key: str, default: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default
