from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
import re

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import (
    canonical_numeric_literal,
    fact_period_literals,
    numeric_literal_spans,
    numeric_literals,
)


_MEMBER_COUNT = re.compile(r"(?P<count>\d{1,3}(?:,\d{3})*)\s*개")
_QUARTER_PERIOD = re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$", re.IGNORECASE)
_MONTH_PERIOD = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})$")
_DATE_PERIOD = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})$"
)
_YEAR_PERIOD = re.compile(r"^(?P<year>\d{4})$")
_BARE_KOREAN_MONTH = re.compile(r"(?<!\d)(?P<month>0?[1-9]|1[0-2])\s*월")


@dataclass(frozen=True, slots=True)
class BareMonthAmbiguity:
    start: int
    end: int
    candidate_count: int


@dataclass(frozen=True, slots=True)
class PeriodSpanResolution:
    spans: tuple[tuple[int, int], ...]
    bare_month_ambiguities: tuple[BareMonthAmbiguity, ...]

    def ambiguity_reason_for(self, start: int, end: int) -> str | None:
        for ambiguity in self.bare_month_ambiguities:
            if ambiguity.start <= start and end <= ambiguity.end:
                return f"ambiguous_period_month_candidates_{ambiguity.candidate_count}"
        return None


def claim_semantic_rejection(
    text: str,
    cited_facts: tuple[V3EvidenceFact, ...],
) -> str | None:
    market_facts = tuple(
        fact for fact in cited_facts if isinstance(fact, MarketMetricFact)
    )
    value_kinds = {
        kind
        for fact in market_facts
        for kind in _value_kinds(fact.raw_result)
    }
    if "observed" in value_kinds and "system_forecast" in value_kinds:
        return "observed_forecast_mixed_claim"
    if "system_forecast" in value_kinds and "시스템 예측" not in text:
        return "forecast_label_missing"
    if "system_simulation" in value_kinds and not any(
        label in text for label in ("시스템 시뮬레이션", "사전 계산 시뮬레이션")
    ):
        return "simulation_label_missing"
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
    hhi_claim_periods = ordered_unique(
        period
        for fact in market_facts
        for period in _hhi_claim_periods(fact)
    )
    hhi_period_value_pairs = tuple(
        (period, value)
        for fact in market_facts
        for period, value in _hhi_period_value_pairs(fact)
    )
    mentioned_hhi_values = _mentioned_hhi_values(text, hhi_period_value_pairs)
    if _mentions_hhi(text) and (
        not hhi_claim_periods
        or (
            mentioned_hhi_values
            and not _hhi_value_mentions_bound_to_periods(
                text,
                period_value_pairs=hhi_period_value_pairs,
                all_periods=hhi_claim_periods,
            )
        )
        or (
            not mentioned_hhi_values
            and not any(_period_mentioned(text, period) for period in hhi_claim_periods)
        )
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
    return period_span_resolution(text, cited_facts).spans


def period_span_resolution(
    text: str,
    cited_facts: tuple[V3EvidenceFact, ...],
) -> PeriodSpanResolution:
    spans: set[tuple[int, int]] = set()
    periods = ordered_unique(
        period
        for fact in cited_facts
        for period in fact_period_literals(fact)
    )
    for period in periods:
        spans.update(_period_spans(text, period))

    month_candidates: dict[int, set[str]] = {}
    for period in periods:
        parsed = _period_month(period)
        if parsed is not None:
            month_candidates.setdefault(parsed, set()).add(period)

    ambiguities: list[BareMonthAmbiguity] = []
    for match in _BARE_KOREAN_MONTH.finditer(text):
        numeric_span = match.span("month")
        if any(start <= numeric_span[0] and numeric_span[1] <= end for start, end in spans):
            continue
        candidates = month_candidates.get(int(match.group("month")), set())
        if len(candidates) == 1:
            spans.add(match.span())
        elif len(candidates) > 1:
            ambiguities.append(
                BareMonthAmbiguity(
                    start=match.start(),
                    end=match.end(),
                    candidate_count=len(candidates),
                )
            )
    return PeriodSpanResolution(
        spans=tuple(sorted(spans)),
        bare_month_ambiguities=tuple(ambiguities),
    )


def _period_spans(text: str, period: str) -> set[tuple[int, int]]:
    spans = {
        (match.start(), match.end())
        for match in re.finditer(
            rf"(?<![A-Za-z0-9]){re.escape(period)}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        )
    }
    normalized = period.strip()
    quarter_match = _QUARTER_PERIOD.fullmatch(normalized)
    if quarter_match is not None:
        year_text = quarter_match.group("year")
        year = re.escape(year_text)
        quarter = re.escape(quarter_match.group("quarter"))
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(rf"{year}\s*년\s*{quarter}\s*분기", text)
        )
        spans.update(_year_spans(text, year_text))
        return spans
    date_match = _DATE_PERIOD.fullmatch(normalized)
    if date_match is not None:
        year_text = date_match.group("year")
        month_number = int(date_match.group("month"))
        day_number = int(date_match.group("day"))
        month = rf"0?{month_number}" if month_number < 10 else str(month_number)
        day = rf"0?{day_number}" if day_number < 10 else str(day_number)
        year = re.escape(year_text)
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(
                rf"{year}\s*년\s*{month}\s*월\s*{day}\s*일",
                text,
            )
        )
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(rf"{year}\s*년\s*{month}\s*월", text)
        )
        spans.update(_year_spans(text, year_text))
        return spans
    month_match = _MONTH_PERIOD.fullmatch(normalized)
    if month_match is not None:
        year_text = month_match.group("year")
        year = re.escape(year_text)
        month_number = int(month_match.group("month"))
        month = rf"0?{month_number}" if month_number < 10 else str(month_number)
        spans.update(
            (match.start(), match.end())
            for match in re.finditer(rf"{year}\s*년\s*{month}\s*월", text)
        )
        spans.update(_year_spans(text, year_text))
        return spans
    year_match = _YEAR_PERIOD.fullmatch(normalized)
    if year_match is not None:
        spans.update(_year_spans(text, year_match.group("year")))
    return spans


def _year_spans(text: str, year: str) -> set[tuple[int, int]]:
    return {
        (match.start(), match.end())
        for match in re.finditer(rf"(?<!\d){re.escape(year)}\s*년?(?!\d)", text)
    }


def _period_month(period: str) -> int | None:
    normalized = period.strip()
    match = _DATE_PERIOD.fullmatch(normalized) or _MONTH_PERIOD.fullmatch(normalized)
    if match is None:
        return None
    month = int(match.group("month"))
    return month if 1 <= month <= 12 else None


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


def _hhi_claim_periods(fact: MarketMetricFact) -> tuple[str, ...]:
    periods = list(_metric_periods(fact, metric="hhi"))
    series = _render_data(fact).get("hhi_series_5y")
    if isinstance(series, Iterable) and not isinstance(series, (str, bytes, Mapping)):
        for item in series:
            if isinstance(item, Mapping) and item.get("period") is not None:
                periods.append(str(item["period"]))
    return ordered_unique(periods)


def _hhi_period_value_pairs(fact: MarketMetricFact) -> tuple[tuple[str, object], ...]:
    render_data = _render_data(fact)
    pairs: list[tuple[str, object]] = []
    latest_periods = _metric_periods(fact, metric="hhi")
    latest_value = render_data.get("hhi_recent")
    if latest_value is None and str(fact.metric or "").casefold() == "hhi":
        latest_value = render_data.get("value")
    if latest_periods and latest_value is not None:
        pairs.append((latest_periods[0], latest_value))
    series = render_data.get("hhi_series_5y")
    if isinstance(series, Iterable) and not isinstance(series, (str, bytes, Mapping)):
        for item in series:
            if not isinstance(item, Mapping) or item.get("period") is None:
                continue
            value = item.get("hhi")
            if value is None:
                value = item.get("value")
            if value is not None:
                pairs.append((str(item["period"]), value))
    return tuple(dict.fromkeys(pairs))


def _mentioned_hhi_values(
    text: str,
    period_value_pairs: tuple[tuple[str, object], ...],
) -> frozenset[str]:
    evidence_values = {
        canonical_numeric_literal(str(value)) for _, value in period_value_pairs
    }
    return frozenset(
        canonical_numeric_literal(literal)
        for literal in numeric_literals(text)
        if canonical_numeric_literal(literal) in evidence_values
    )


def _hhi_value_mentions_bound_to_periods(
    text: str,
    *,
    period_value_pairs: tuple[tuple[str, object], ...],
    all_periods: tuple[str, ...],
) -> bool:
    evidence_pairs = {
        (period, canonical_numeric_literal(str(value)))
        for period, value in period_value_pairs
    }
    evidence_values = {value for _, value in evidence_pairs}
    value_occurrences = tuple(
        (canonical_numeric_literal(literal), (start, end))
        for literal, start, end in numeric_literal_spans(text)
        if canonical_numeric_literal(literal) in evidence_values
    )
    period_occurrences = _period_occurrences(text, all_periods)
    mentioned_periods = tuple(period for period, _ in period_occurrences)
    if "각각" in text:
        if len(mentioned_periods) != len(value_occurrences):
            return False
        return all(
            (period, value) in evidence_pairs
            for period, (value, _) in zip(mentioned_periods, value_occurrences)
        )
    if len(mentioned_periods) > 1 and len(value_occurrences) == 1:
        value = value_occurrences[0][0]
        return all((period, value) in evidence_pairs for period in mentioned_periods)
    for value, value_span in value_occurrences:
        distances = tuple(
            (period, span, _span_distance(span, value_span))
            for period, span in period_occurrences
        )
        if not distances:
            return False
        nearest_distance = min(distance for _, _, distance in distances)
        nearest_periods = {
            period for period, _, distance in distances if distance == nearest_distance
        }
        if not any((period, value) in evidence_pairs for period in nearest_periods):
            return False
    return bool(value_occurrences)


def _period_occurrences(
    text: str,
    periods: tuple[str, ...],
) -> tuple[tuple[str, tuple[int, int]], ...]:
    occurrences = tuple(
        (period, span)
        for period in periods
        for span in _period_spans(text, period)
    )
    specific_occurrences = tuple(
        occurrence
        for occurrence in occurrences
        if not any(
            other_span != occurrence[1]
            and other_span[0] <= occurrence[1][0]
            and occurrence[1][1] <= other_span[1]
            for _, other_span in occurrences
        )
    )
    resolved_occurrences = set(specific_occurrences)
    month_candidates: dict[int, set[str]] = {}
    for period in periods:
        month = _period_month(period)
        if month is not None:
            month_candidates.setdefault(month, set()).add(period)
    for match in _BARE_KOREAN_MONTH.finditer(text):
        month_span = match.span()
        if any(
            start <= month_span[0] and month_span[1] <= end
            for _, (start, end) in resolved_occurrences
        ):
            continue
        candidates = month_candidates.get(int(match.group("month")), set())
        if len(candidates) == 1:
            resolved_occurrences.add((next(iter(candidates)), month_span))
    return tuple(sorted(resolved_occurrences, key=lambda item: item[1]))


def _span_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    if left[1] <= right[0]:
        return right[0] - left[1]
    if right[1] <= left[0]:
        return left[0] - right[1]
    return 0


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
    return bool(_period_spans(text, period))


def _object_mapping(value: object) -> Mapping[str, object]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _value_kinds(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        kinds = (
            (str(value["value_kind"]),)
            if isinstance(value.get("value_kind"), str)
            else ()
        )
        return kinds + tuple(
            kind for item in value.values() for kind in _value_kinds(item)
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return tuple(kind for item in value for kind in _value_kinds(item))
    return ()


__all__ = [
    "BareMonthAmbiguity",
    "PeriodSpanResolution",
    "claim_semantic_rejection",
    "ordered_unique",
    "period_numeric_spans",
    "period_span_resolution",
]
