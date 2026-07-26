from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import source_label, table


MISSING_LABEL: Final[str] = "—"
ALL_CHANNELS_LABEL: Final[str] = "전체"
PROVENANCE_HEADERS: Final[tuple[str, ...]] = (
    "출처",
    "기준기간",
    "뷰",
    "시장정의",
    "분모",
    "채널",
    "단위",
)

_INTERNAL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_])(?:ml|strategy|cd|competitive)_\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_INTERNAL_TOOL_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_])tool_call_\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_PROVENANCE_FALLBACK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_])series(?![A-Za-z0-9_])|확정 시장",
    re.IGNORECASE,
)
_PERIOD_RE: Final[re.Pattern[str]] = re.compile(r"20\d{2}-(?:\d{2}|Q[1-4])")
_ATC4_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]\d{2}[A-Z]\d$")


@dataclass(frozen=True, slots=True)
class ProvenanceRow:
    source: str = MISSING_LABEL
    period: str = MISSING_LABEL
    view: str = MISSING_LABEL
    market: str = MISSING_LABEL
    denominator: str = MISSING_LABEL
    channel: str = ALL_CHANNELS_LABEL
    unit: str = MISSING_LABEL

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.source,
            self.period,
            self.view,
            self.market,
            self.denominator,
            self.channel,
            self.unit,
        )


def sanitize_internal_provenance_labels(text: str) -> str:
    """Block internal IDs globally and fallback labels only inside provenance."""

    sanitized = _INTERNAL_TOOL_CALL_RE.sub(MISSING_LABEL, _INTERNAL_ID_RE.sub(MISSING_LABEL, text))
    lines: list[str] = []
    in_source_section = False
    for raw_line in sanitized.splitlines():
        stripped = raw_line.strip()
        if re.fullmatch(r"#{1,6}\s*출처", stripped):
            in_source_section = True
        elif in_source_section and re.match(r"#{1,6}\s+\S", stripped):
            in_source_section = False
        line = _PROVENANCE_FALLBACK_RE.sub(MISSING_LABEL, raw_line) if in_source_section else raw_line
        lines.append(re.sub(rf"{re.escape(MISSING_LABEL)}(?:\s+{re.escape(MISSING_LABEL)})+", MISSING_LABEL, line))
    return "\n".join(lines)


def public_source(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return MISSING_LABEL
    if value == "conversation_context":
        return "직전 턴 검증 fact"
    label = source_label(value)
    if label in {"IQVIA", "IQVIA NSA"}:
        return "IQVIA NSA"
    return public_value(label)


def public_view(raw_view: Any, raw_market: Any) -> str:
    view = str(raw_view or "").strip().lower()
    market = str(raw_market or "").strip()
    if view == "파일":
        return "파일"
    if view in {"market_landscape", "strategic_ml"} or re.fullmatch(
        r"(?:ml|strategy)_\d+", market, re.IGNORECASE
    ):
        return "전략뷰 (market_landscape)"
    if view in {"competitive_dynamics", "strategic_cd"} or re.fullmatch(
        r"(?:cd|competitive)_\d+", market, re.IGNORECASE
    ):
        return "전략뷰 (competitive_dynamics)"
    if "competitive_dynamics" in view:
        return "전략뷰 (competitive_dynamics)"
    if "market_landscape" in view:
        return "전략뷰 (market_landscape)"
    if view in {"general", "general_view"} or _ATC4_RE.fullmatch(market):
        return "일반뷰 (ATC4)"
    return MISSING_LABEL


def public_market(raw_name: Any, raw_market: Any) -> str:
    name = str(raw_name or "").strip()
    if name and not contains_internal_label(name):
        return public_value(name)
    market = str(raw_market or "").strip()
    if _ATC4_RE.fullmatch(market):
        return f"ATC4 {market}"
    return MISSING_LABEL


def period_tokens(value: Any) -> list[str]:
    text = str(value)
    tokens = _PERIOD_RE.findall(text)
    if tokens:
        return tokens
    return re.findall(r"(?<!\d)(20\d{2})(?![\d-])", text)


def period_range(periods: Sequence[str]) -> str:
    if not periods:
        return MISSING_LABEL
    if len(periods) == 1:
        return periods[0]
    return f"{periods[0]}~{periods[-1]}"


def contains_internal_label(text: str) -> bool:
    return bool(
        _INTERNAL_ID_RE.search(text)
        or _INTERNAL_TOOL_CALL_RE.search(text)
        or _PROVENANCE_FALLBACK_RE.search(text)
    )


def public_value(value: Any, *, missing: str = MISSING_LABEL) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text in {"", "-", MISSING_LABEL, "None", "null"}:
        return missing
    if contains_internal_label(text):
        return missing
    return text


def normalized_row(
    source: Any = MISSING_LABEL,
    period: Any = MISSING_LABEL,
    view: Any = MISSING_LABEL,
    market: Any = MISSING_LABEL,
    denominator: Any = MISSING_LABEL,
    channel: Any = ALL_CHANNELS_LABEL,
    unit: Any = MISSING_LABEL,
) -> ProvenanceRow:
    public_market_label = public_value(market)
    return ProvenanceRow(
        source=public_source(source),
        period=public_value(period),
        view=_public_scoped_view(view, public_market_label),
        market=public_market_label,
        denominator=public_value(denominator),
        channel=public_value(channel, missing=ALL_CHANNELS_LABEL),
        unit=public_value(unit),
    )


def _public_scoped_view(raw_view: Any, public_market_label: str) -> str:
    view = public_value(raw_view)
    if not view.startswith("전략뷰 (") or public_market_label == MISSING_LABEL:
        return view
    suffix = f" · {public_market_label}"
    return view if view.endswith(suffix) else f"{view}{suffix}"


def dedupe_rows(rows: Sequence[ProvenanceRow]) -> tuple[ProvenanceRow, ...]:
    clean = tuple(dict.fromkeys(normalized_row(*row.as_tuple()) for row in rows))
    return clean or (ProvenanceRow(),)


def merge_public_source_rows(rows: Sequence[ProvenanceRow]) -> tuple[ProvenanceRow, ...]:
    """Merge public rows only within the same source, view, and public market."""

    groups: dict[tuple[str, str, str], list[ProvenanceRow]] = {}
    for row in dedupe_rows(rows):
        clean = normalized_row(*row.as_tuple())
        groups.setdefault((clean.source, clean.view, clean.market), []).append(clean)

    merged = tuple(_merge_source_group(group) for group in groups.values())
    return merged or (ProvenanceRow(),)


def _merge_source_group(rows: Sequence[ProvenanceRow]) -> ProvenanceRow:
    raw_periods = sorted(
        {row.period for row in rows if row.period != MISSING_LABEL},
        key=lambda value: (value.casefold(), value),
    )
    if len(raw_periods) == 1:
        merged_period = raw_periods[0]
    else:
        period_values = sorted(
            {token for period in raw_periods for token in period_tokens(period)},
            key=lambda value: (value.casefold(), value),
        )
        merged_period = period_range(period_values)
    return ProvenanceRow(
        source=rows[0].source,
        period=merged_period,
        view=rows[0].view,
        market=_merge_values(tuple(row.market for row in rows)),
        denominator=_merge_values(tuple(row.denominator for row in rows)),
        channel=_merge_values(tuple(row.channel for row in rows)),
        unit=_merge_values(tuple(row.unit for row in rows)),
    )


def _merge_values(values: Sequence[str], *, missing: str = MISSING_LABEL) -> str:
    clean = sorted(
        {
            public_value(value, missing=missing)
            for value in values
            if public_value(value, missing=missing) != missing
        },
        key=lambda value: (value.casefold(), value),
    )
    if not clean:
        return missing
    separator = os.getenv("JW_CHAT_PROVENANCE_MULTI_VALUE_SEPARATOR", ", ")
    limit = max(2, int(os.getenv("JW_CHAT_PROVENANCE_MULTI_VALUE_LIMIT", "3")))
    if len(clean) <= limit:
        return separator.join(clean)
    displayed = clean[: limit - 1]
    return f"{separator.join(displayed)} 외 {len(clean) - len(displayed)}"


def render_provenance_table(title: str, rows: Sequence[ProvenanceRow]) -> str:
    return table(title, PROVENANCE_HEADERS, tuple(row.as_tuple() for row in dedupe_rows(rows)))
