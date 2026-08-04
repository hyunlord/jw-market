from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
import re

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import fact_period_literals


_MEMBER_COUNT = re.compile(r"(?P<count>\d{1,3}(?:,\d{3})*)\s*개")
_QUARTER_PERIOD = re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$", re.IGNORECASE)


def claim_semantic_rejection(
    text: str,
    cited_facts: tuple[V3EvidenceFact, ...],
) -> str | None:
    market_facts = tuple(
        fact for fact in cited_facts if isinstance(fact, MarketMetricFact)
    )
    hhi_periods = ordered_unique(
        period
        for fact in market_facts
        for period in _metric_periods(fact, metric="hhi")
    )
    market_size_periods = ordered_unique(
        period
        for fact in market_facts
        for period in _metric_periods(fact, metric="market_size")
    )
    if hhi_periods and market_size_periods and set(hhi_periods) != set(market_size_periods):
        return "market_hhi_period_mismatch"
    if _mentions_hhi(text) and (
        not hhi_periods or any(not _period_mentioned(text, period) for period in hhi_periods)
    ):
        return "hhi_period_missing"
    for fact in market_facts:
        reason = _population_rejection(text, _render_data(fact))
        if reason is not None:
            return reason
    return None


def ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def period_numeric_spans(
    text: str,
    cited_facts: tuple[V3EvidenceFact, ...],
) -> tuple[tuple[int, int], ...]:
    spans: set[tuple[int, int]] = set()
    for fact in cited_facts:
        for period in fact_period_literals(fact):
            spans.update(_period_spans(text, period))
    return tuple(sorted(spans))


def _period_spans(text: str, period: str) -> set[tuple[int, int]]:
    spans = {
        (match.start(), match.end())
        for match in re.finditer(
            rf"(?<![A-Za-z0-9]){re.escape(period)}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        )
    }
    quarter_match = _QUARTER_PERIOD.fullmatch(period.strip())
    if quarter_match is not None:
        year = re.escape(quarter_match.group("year"))
        quarter = re.escape(quarter_match.group("quarter"))
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(rf"{year}\s*년\s*{quarter}\s*분기", text)
        )
        return spans
    month_match = re.fullmatch(r"(?P<year>\d{4})-(?P<month>\d{1,2})", period)
    if month_match is not None:
        year = re.escape(month_match.group("year"))
        month = re.escape(str(int(month_match.group("month"))))
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(rf"{year}\s*년\s*{month}\s*월", text)
        )
    return spans


def _metric_periods(fact: MarketMetricFact, *, metric: str) -> tuple[str, ...]:
    render_data = _render_data(fact)
    fact_metric = str(fact.metric or "").casefold()
    if metric == "hhi":
        has_metric = fact_metric == "hhi" or render_data.get("hhi_recent") is not None
        period = render_data.get("hhi_period") or fact.period
    else:
        has_metric = (
            fact_metric == "market_size"
            or render_data.get("market_size_recent_krw") is not None
        )
        period = render_data.get("market_size_period") or fact.period
    return (str(period),) if has_metric and period is not None else ()


def _render_data(fact: MarketMetricFact) -> Mapping[str, object]:
    raw = _object_mapping(fact.raw_result)
    envelope_raw = raw.get("raw")
    if isinstance(envelope_raw, Mapping):
        raw = envelope_raw
    render_data = raw.get("render_data")
    return render_data if isinstance(render_data, Mapping) else {}


def _population_rejection(
    text: str,
    render_data: Mapping[str, object],
) -> str | None:
    if "브랜드" not in text:
        return None
    counts = tuple(
        int(match.group("count").replace(",", ""))
        for match in _MEMBER_COUNT.finditer(text)
    )
    layer = _population_layer(text)
    if not counts:
        return (
            "population_layer_unspecified"
            if "구성 브랜드" in text and layer is None
            else None
        )
    population_counts = {
        "full": _optional_int(render_data.get("member_population_count")),
        "active": _optional_int(render_data.get("active_member_count")),
        "display": _optional_int(render_data.get("display_member_count")),
    }
    if all(value is None for value in population_counts.values()):
        return None
    if layer is None:
        return "population_layer_unspecified"
    expected = population_counts[layer]
    if expected is None:
        return "population_layer_unavailable"
    return None if all(count == expected for count in counts) else "population_layer_mismatch"


def _population_layer(text: str) -> str | None:
    if any(token in text for token in ("활성", "양수 실적")):
        return "active"
    if any(token in text for token in ("화면 표시", "표시 브랜드", "상위")):
        return "display"
    if any(token in text for token in ("전체", "mart 관측", "마트 관측", "시장에")):
        return "full"
    return None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _mentions_hhi(text: str) -> bool:
    return "HHI" in text.upper() or "시장 집중도" in text


def _period_mentioned(text: str, period: str) -> bool:
    if period in text:
        return True
    match = _QUARTER_PERIOD.fullmatch(period.strip())
    if match is None:
        return False
    year = re.escape(match.group("year"))
    quarter = re.escape(match.group("quarter"))
    return re.search(rf"{year}\s*년\s*{quarter}\s*분기", text) is not None


def _object_mapping(value: object) -> Mapping[str, object]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "claim_semantic_rejection",
    "ordered_unique",
    "period_numeric_spans",
]
