from __future__ import annotations

import re

_INTERNAL_MARKET_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:ml|strategy|cd|competitive)_\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_INTERNAL_TOOL_ID_RE = re.compile(r"(?<![A-Za-z0-9_])tool_call_\d+(?![A-Za-z0-9_])", re.IGNORECASE)
_SERIES_RE = re.compile(r"(?<![A-Za-z0-9_])series(?![A-Za-z0-9_])", re.IGNORECASE)


def is_internal_market_id(value: str) -> bool:
    return bool(_INTERNAL_MARKET_ID_RE.fullmatch(value.strip()))


def public_market_label(*values: str) -> str:
    for value in values:
        label = value.strip()
        if label and not is_internal_market_id(label):
            return label
    return "해당 시장"


def public_axis_label(value: str) -> str:
    label = value.strip()
    return "시계열" if label.lower() == "series" else label or "-"


def sanitize_provenance_labels(text: str) -> str:
    sanitized = _INTERNAL_MARKET_ID_RE.sub("해당 시장", text)
    sanitized = _INTERNAL_TOOL_ID_RE.sub("—", sanitized)
    sanitized = _SERIES_RE.sub("시계열", sanitized)
    return re.sub(r"(?<![가-힣])확정 시장(?![가-힣])", "해당 시장", sanitized)
