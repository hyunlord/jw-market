from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# allow: SIZE_OK - keeps one compatibility boundary's parsing and tuple evaluation together.

_DIRECT_COMPARISON_HEADING_RE = re.compile(
    r"(?m)^##\s+브랜드\s+(?:(?:매출|점유율|순위)\s+)?비교\s*$"
)
_MONTH_RE = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
_QUARTER_RE = re.compile(r"^20\d{2}-Q[1-4]$")
_YEAR_RE = re.compile(r"^20\d{2}$")
_BRAND_PREFIX_SEPARATORS = (
    " ", ",", ";", "/", "+", "&", "(", ")", "과", "와", "이랑", "랑", "하고", "및",
)
_BRAND_SUFFIX_SEPARATORS = (*_BRAND_PREFIX_SEPARATORS, ".", "의", "은", "는", "을", "를", "도", "대비")


@dataclass(frozen=True, slots=True)
class ComparisonCompatibilityDecision:
    brands: tuple[str, ...]
    mismatch_axes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ComparisonSignature:
    brand: str
    source: str
    grain: str
    period: tuple[str, ...]
    metrics: frozenset[str]
    metric_units: tuple[tuple[str, str], ...]
    market_definition: str
    denominator: str


def incompatible_direct_comparison(
    question: str,
    answer: str,
    calls: Sequence[Mapping[str, Any]],
) -> ComparisonCompatibilityDecision | None:
    """Return observed incompatibilities only for a released direct-comparison table."""

    has_comparison_heading = _DIRECT_COMPARISON_HEADING_RE.search(answer) is not None
    if not has_comparison_heading and not _direct_comparison_requested(question):
        return None
    requested_metrics = _requested_metrics(question)
    if not requested_metrics:
        return None

    candidate_signatures = tuple(
        signature
        for call in calls
        if (signature := _signature_from_call(call, requested_metrics)) is not None
    )
    compared_brands = (
        _comparison_table_brands(answer)
        if has_comparison_heading
        else _explicit_brand_mentions(
            question,
            tuple(signature.brand for signature in candidate_signatures),
        )
    )
    signatures = tuple(
        signature
        for signature in candidate_signatures
        if signature.brand in compared_brands
    )
    comparable = tuple(dict.fromkeys(signatures))
    brands = tuple(dict.fromkeys(signature.brand for signature in comparable))
    if len(brands) < 2:
        return None
    if not any(
        signature.source or signature.market_definition or signature.denominator
        for signature in comparable
    ):
        # Old in-memory fixtures predate provenance fields. They cannot establish
        # compatibility, but changing them here would alter unrelated legacy answers.
        return None
    mismatch_axes = _mismatch_axes(
        comparable,
        brands=brands,
        requested_metrics=requested_metrics,
    )
    if not mismatch_axes:
        return None
    return ComparisonCompatibilityDecision(
        brands=brands,
        mismatch_axes=mismatch_axes,
    )


def _signature_from_call(
    call: Mapping[str, Any],
    requested_metrics: frozenset[str],
) -> _ComparisonSignature | None:
    if str(call.get("tool") or "") != "get_brand_metric":
        return None
    data_value = call.get("render_data")
    if not isinstance(data_value, Mapping):
        return None
    data = data_value
    if _call_failed(call, data):
        return None

    series_value = data.get("brand_value_series_10pt")
    if not isinstance(series_value, Sequence) or isinstance(series_value, str | bytes):
        return None
    points = tuple(point for point in series_value if isinstance(point, Mapping))
    if not points:
        return None

    query_spec_value = data.get("query_spec")
    query_spec = query_spec_value if isinstance(query_spec_value, Mapping) else {}
    brand = _first_text(data, query_spec, call, keys=("brand", "canonical_brand", "brand_name"))
    source = _normalized_text(_first_text(data, call, query_spec, keys=("source_label", "source")))
    periods = tuple(
        sorted(
            dict.fromkeys(
                str(point.get("period") or "").strip()
                for point in points
                if str(point.get("period") or "").strip()
            )
        )
    )
    grain = _period_grain(periods)
    available_metrics = _available_metrics(points, data)
    market_definition = _first_text(
        query_spec,
        data,
        keys=("market_display_name", "market_name", "market_definition"),
    )
    market_definition = _normalized_text(market_definition)
    denominator = _first_text(
        query_spec,
        data,
        keys=(
            "total_brands_in_market",
            "denominator",
            "rank_denominator",
            "market_brand_count",
            "inherited_denominator",
        ),
    )
    denominator = _normalized_text(denominator)

    if not brand:
        return None

    present_metrics = requested_metrics & available_metrics
    if not present_metrics:
        return None
    metric_units = tuple(
        sorted(
            (metric, _metric_unit(metric, data, points))
            for metric in present_metrics
        )
    )
    return _ComparisonSignature(
        brand=brand,
        source=source,
        grain=grain,
        period=periods,
        metrics=frozenset(requested_metrics & available_metrics),
        metric_units=metric_units,
        market_definition=market_definition,
        denominator=denominator,
    )


def _call_failed(call: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
    status = str(call.get("status") or data.get("status") or "").casefold()
    return status in {"empty", "error", "failed", "no_data", "query_failed", "timeout"}


def _requested_metrics(question: str) -> frozenset[str]:
    normalized = question.casefold()
    metrics: set[str] = set()
    if "매출" in normalized:
        metrics.add("sales")
    if "점유율" in normalized or re.search(r"(?<![a-z])ms(?![a-z])", normalized):
        metrics.add("share")
    if "순위" in normalized:
        metrics.add("rank")
    return frozenset(metrics)


def _direct_comparison_requested(question: str) -> bool:
    normalized = question.casefold()
    if "비교" in normalized or "대비" in normalized:
        return True
    if re.search(r"(?<![a-z])vs\.?(?![a-z])", normalized):
        return True
    separated = any(marker in normalized for marker in ("원천별", "분리", "따로"))
    return "각각" in normalized and not separated


def _comparison_table_brands(answer: str) -> frozenset[str]:
    brands: set[str] = set()
    in_comparison = False
    for raw_line in answer.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_comparison = _DIRECT_COMPARISON_HEADING_RE.fullmatch(line) is not None
            continue
        if not in_comparison or not line.startswith("|"):
            continue
        first_cell = line.strip("|").split("|", maxsplit=1)[0].strip()
        if first_cell != "브랜드" and first_cell.strip("-: "):
            brands.add(first_cell)
    return frozenset(brands)


def _explicit_brand_mentions(
    question: str,
    candidate_brands: tuple[str, ...],
) -> frozenset[str]:
    return frozenset(
        brand
        for brand in dict.fromkeys(candidate_brands)
        if _has_delimited_brand_mention(question, brand)
    )


def _has_delimited_brand_mention(question: str, brand: str) -> bool:
    for match in re.finditer(re.escape(brand), question):
        left = question[: match.start()]
        right = question[match.end() :]
        left_delimited = not left or left.endswith(_BRAND_PREFIX_SEPARATORS)
        right_delimited = not right or right.startswith(_BRAND_SUFFIX_SEPARATORS)
        if left_delimited and right_delimited:
            return True
    return False


def _available_metrics(
    points: tuple[Mapping[str, Any], ...],
    data: Mapping[str, Any],
) -> frozenset[str]:
    metrics: set[str] = set()
    if all(any(key in point for key in ("value_krw", "value_억원", "sales", "sales_krw")) for point in points):
        metrics.add("sales")
    if all(any(key in point for key in ("ms_pct", "market_share", "share", "share_pct")) for point in points):
        metrics.add("share")
    if all("rank" in point for point in points) or data.get("rank") not in (None, ""):
        metrics.add("rank")
    return frozenset(metrics)


def _metric_unit(
    metric: str,
    data: Mapping[str, Any],
    points: tuple[Mapping[str, Any], ...],
) -> str:
    explicit = str(data.get(f"{metric}_unit") or "").strip().casefold()
    if explicit:
        return _canonical_unit(explicit)
    if metric == "sales" and any(
        any(key in point for key in ("value_krw", "sales_krw")) for point in points
    ):
        return "krw"
    if metric == "sales":
        return _canonical_unit(str(data.get("unit_label") or data.get("unit") or "억원"))
    if metric == "share":
        return "%"
    if metric == "rank":
        return "rank"
    return ""


def _canonical_unit(value: str) -> str:
    normalized = value.strip().casefold().replace(" ", "")
    if normalized in {"krw", "원"}:
        return "krw"
    if normalized == "억원":
        return "krw:100000000"
    if normalized == "백만원":
        return "krw:1000000"
    if normalized in {"%", "percent", "pct", "퍼센트"}:
        return "%"
    return normalized


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _period_grain(periods: tuple[str, ...]) -> str:
    grains = {
        "month" if _MONTH_RE.fullmatch(period) else
        "quarter" if _QUARTER_RE.fullmatch(period) else
        "year" if _YEAR_RE.fullmatch(period) else
        ""
        for period in periods
    }
    grains.discard("")
    return next(iter(grains)) if len(grains) == 1 else ""


def _mismatch_axes(
    signatures: tuple[_ComparisonSignature, ...],
    *,
    brands: tuple[str, ...],
    requested_metrics: frozenset[str],
) -> tuple[str, ...]:
    axes: list[str] = []
    if any(
        frozenset(
            metric
            for signature in signatures
            if signature.brand == brand
            for metric in signature.metrics
        )
        != requested_metrics
        for brand in brands
    ):
        axes.append("metric")

    for axis in ("source", "grain", "period", "unit", "market_definition", "denominator"):
        if any(
            _metric_axis_mismatch(
                axis,
                metric=metric,
                signatures=signatures,
                brands=brands,
            )
            for metric in requested_metrics
        ):
            axes.append(axis)
    return tuple(axes)


def _metric_axis_mismatch(
    axis: str,
    *,
    metric: str,
    signatures: tuple[_ComparisonSignature, ...],
    brands: tuple[str, ...],
) -> bool:
    metric_signatures = tuple(
        signature for signature in signatures if metric in signature.metrics
    )
    represented_brands = frozenset(signature.brand for signature in metric_signatures)
    if represented_brands != frozenset(brands):
        return False
    values = tuple(
        _metric_unit_from_signature(signature, metric)
        if axis == "unit"
        else getattr(signature, axis)
        for signature in metric_signatures
    )
    return any(not value for value in values) or len(set(values)) > 1


def _metric_unit_from_signature(
    signature: _ComparisonSignature,
    metric: str,
) -> str:
    return next(
        (
            unit
            for candidate_metric, unit in signature.metric_units
            if candidate_metric == metric
        ),
        "",
    )


def _first_text(*containers: Mapping[str, Any], keys: Sequence[str]) -> str:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], ()):
                return str(value).strip()
    return ""
