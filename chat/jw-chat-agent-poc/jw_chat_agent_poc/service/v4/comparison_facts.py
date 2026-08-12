from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import SourceResult


_DISPLAY_QUANTUM = Decimal("0.01")


def build_comparison_facts(results: Sequence[SourceResult]) -> dict[str, Any]:
    """Build deterministic comparison facts from a same-scope mart bundle."""

    calls = _mart_calls(results)
    bundle = _find_bundle(calls)
    if bundle is None:
        bundle = _legacy_bundle(calls)
    if bundle is None:
        return {}

    period_start = str(bundle.get("period_start") or "").strip()
    period_end = str(bundle.get("period_end") or "").strip()
    members = bundle.get("members")
    if not period_start or not period_end or not isinstance(members, list):
        return {}

    requested_period = str(bundle.get("requested_period") or "").strip()
    period_constrained = requested_period not in {"", "latest"}
    market_values: dict[str, Decimal] = {}
    for call in calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        market_values = _period_values(render_data.get("market_size_series"))
        if period_start in market_values and period_end in market_values:
            break

    deltas: list[dict[str, Any]] = []
    numeric_deltas: list[tuple[str, Decimal]] = []
    target_values: tuple[str, Decimal, Decimal] | None = None
    target_share_delta: Decimal | None = None
    competitor_share_changes: list[dict[str, str]] = []
    for member in members:
        if not isinstance(member, Mapping):
            continue
        brand = str(member.get("brand") or "").strip()
        role = str(member.get("role") or "").strip()
        render_data = member.get("render_data")
        if not brand or not isinstance(render_data, Mapping):
            continue
        series = render_data.get("brand_value_series_10pt") or render_data.get("series")
        values = _period_values(series)
        start = values.get(period_start)
        end = values.get(period_end)
        derived_share_delta: Decimal | None = None
        if start is not None and end is not None:
            display_start = _display_decimal(start)
            display_end = _display_decimal(end)
            display_delta = display_end - display_start
            deltas.append(
                {
                    "brand": brand,
                    "role": role,
                    "start": _fixed_display(display_start, "억원"),
                    "end": _fixed_display(display_end, "억원"),
                    "delta": _fixed_display(display_delta, "억원", signed=True),
                }
            )
            numeric_deltas.append((brand, display_delta))
            if role == "target":
                target_values = (brand, display_start, display_end)
            derived_share_delta = _period_share_delta(
                display_start,
                display_end,
                market_values.get(period_start),
                market_values.get(period_end),
            )
            if role == "target" and period_constrained:
                target_share_delta = derived_share_delta
        share_delta = (
            derived_share_delta
            if period_constrained and derived_share_delta is not None
            else _decimal_value(member.get("share_delta_pctp"))
        )
        if role == "competitor" and share_delta is not None:
            competitor_share_changes.append(
                {
                    "brand": brand,
                    "change": _fixed_display(share_delta, "%p", signed=True),
                }
            )

    positives = [(brand, value) for brand, value in numeric_deltas if value > 0]
    negatives = [(brand, value) for brand, value in numeric_deltas if value < 0]
    symmetric_pairs: list[dict[str, str]] = []
    remaining = list(negatives)
    for increase_brand, increase in positives:
        if not remaining:
            break
        decrease_brand, decrease = min(
            remaining,
            key=lambda item: abs(abs(increase) - abs(item[1])),
        )
        remaining.remove((decrease_brand, decrease))
        symmetric_pairs.append(
            {
                "increase_brand": increase_brand,
                "increase_delta": _fixed_display(increase, "억원", signed=True),
                "decrease_brand": decrease_brand,
                "decrease_delta": _fixed_display(decrease, "억원", signed=True),
            }
        )

    share_direction: dict[str, str] = {}
    if target_values is not None:
        brand, brand_start, brand_end = target_values
        market_start = market_values.get(period_start)
        market_end = market_values.get(period_end)
        brand_growth = _growth_pct(brand_start, brand_end)
        market_growth = (
            _growth_pct(_display_decimal(market_start), _display_decimal(market_end))
            if market_start is not None and market_end is not None
            else None
        )
        if brand_growth is not None and market_growth is not None:
            direction = (
                "상승"
                if brand_growth > market_growth
                else "하락"
                if brand_growth < market_growth
                else "유지"
            )
            brand_growth_display = _fixed_display(brand_growth, "%", signed=True)
            market_growth_display = _fixed_display(market_growth, "%", signed=True)
            growth_comparison = (
                f"시장 성장률 {market_growth_display}과 같아"
                if direction == "유지"
                else (
                    f"시장 성장률 {market_growth_display}보다 "
                    f"{'높아' if direction == '상승' else '낮아'}"
                )
            )
            share_direction = {
                "brand": brand,
                "brand_growth": brand_growth_display,
                "market_growth": market_growth_display,
                "direction": direction,
                "statement": (
                    f"{brand} 성장률 {brand_growth_display}가 {growth_comparison} "
                    f"점유율 방향은 {direction}입니다."
                ),
            }
            if target_share_delta is not None:
                share_delta_display = _fixed_display(
                    target_share_delta,
                    "%p",
                    signed=True,
                )
                share_direction["share_delta"] = share_delta_display
                share_direction["statement"] = (
                    f"{brand} 성장률 {brand_growth_display}가 {growth_comparison} "
                    f"점유율 변화는 {share_delta_display}이고 점유율 방향은 {direction}입니다."
                )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "brand_deltas": deltas,
        "symmetric_pairs": symmetric_pairs,
        "share_direction": share_direction,
        "competitor_share_changes": competitor_share_changes,
    }


def comparison_numeric_tokens(results: Sequence[SourceResult]) -> set[str]:
    """Return numeric tokens that were deterministically derived by this module."""

    facts = build_comparison_facts(results)
    tokens: set[str] = set()
    for value in _walk_values(facts):
        if not isinstance(value, str):
            continue
        for token in _number_tokens(value):
            tokens.add(token)
    return tokens


def symmetric_observation(
    results: Sequence[SourceResult],
    *,
    entities: tuple[str, str] | None = None,
) -> str | None:
    facts = build_comparison_facts(results)
    pairs = facts.get("symmetric_pairs")
    if not isinstance(pairs, list) or not pairs:
        return None
    pair = pairs[0]
    if entities is not None:
        expected = {entity.casefold() for entity in entities}
        pair = next(
            (
                candidate
                for candidate in pairs
                if {
                    str(candidate.get("increase_brand") or "").casefold(),
                    str(candidate.get("decrease_brand") or "").casefold(),
                }
                == expected
            ),
            None,
        )
        if pair is None:
            return None
    return (
        f"{pair['increase_brand']}{_topic_particle(str(pair['increase_brand']))} "
        f"{pair['increase_delta']}, "
        f"{pair['decrease_brand']}{_topic_particle(str(pair['decrease_brand']))} "
        f"{pair['decrease_delta']}으로 "
        "반대 방향의 변화가 관측됐습니다. 증감 규모는 유사하지만, "
        "환자·처방자 수준의 직접 이동 여부는 현재 자료로 확인되지 않습니다."
    )


def _mart_calls(results: Sequence[SourceResult]) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    for result in results:
        if result.source != "mart" or not isinstance(result.payload, Mapping):
            continue
        raw_calls = result.payload.get("calls")
        if isinstance(raw_calls, list):
            calls.extend(call for call in raw_calls if isinstance(call, Mapping))
        else:
            calls.append(result.payload)
    index = 0
    while index < len(calls):
        nested = calls[index].get("tool_calls")
        if isinstance(nested, list):
            calls.extend(call for call in nested if isinstance(call, Mapping))
        index += 1
    return calls


def _find_bundle(calls: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next(
        (
            bundle
            for call in calls
            for bundle in (call.get("entity_bundle"),)
            if isinstance(bundle, Mapping)
        ),
        None,
    )


def _legacy_bundle(calls: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for call in calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        anchor_brand = str(render_data.get("anchor_brand") or "").strip()
        rows = render_data.get("competitor_rows")
        if not anchor_brand or not isinstance(rows, list):
            continue
        points: list[tuple[str, Decimal, Decimal]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("brand") or "").strip() != anchor_brand:
                continue
            period = str(row.get("period") or "").strip()
            sales_krw = _decimal_value(row.get("sales_krw"))
            share_pct = _decimal_value(row.get("share_pct"))
            if period and sales_krw is not None and share_pct is not None and share_pct > 0:
                points.append((period, sales_krw, share_pct))
        points.sort(key=lambda point: point[0])
        if len(points) < 2 or points[0][0] == points[-1][0]:
            continue
        period_start, sales_start, share_start = points[0]
        period_end, sales_end, share_end = points[-1]
        krw_per_eok = Decimal("100000000")
        calls.append(
            {
                "render_data": {
                    "market_size_series": [
                        {
                            "period": period_start,
                            "value_억원": sales_start * 100 / share_start / krw_per_eok,
                        },
                        {
                            "period": period_end,
                            "value_억원": sales_end * 100 / share_end / krw_per_eok,
                        },
                    ]
                }
            }
        )
        return {
            "anchor": anchor_brand,
            "period_start": period_start,
            "period_end": period_end,
            "same_period_and_denominator": True,
            "members": [
                {
                    "brand": anchor_brand,
                    "role": "target",
                    "share_delta_pctp": share_end - share_start,
                    "render_data": {
                        "brand_value_series_10pt": [
                            {"period": period_start, "value_억원": sales_start / krw_per_eok},
                            {"period": period_end, "value_억원": sales_end / krw_per_eok},
                        ]
                    },
                }
            ],
        }
    return None


def _period_values(series: Any) -> dict[str, Decimal]:
    if not isinstance(series, list):
        return {}
    values: dict[str, Decimal] = {}
    for point in series:
        if not isinstance(point, Mapping):
            continue
        period = str(point.get("period") or "").strip()
        value = _decimal_value(point.get("value_억원"))
        if period and value is not None:
            values[period] = value
    return values


def _display_decimal(value: Decimal) -> Decimal:
    return value.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)


def _growth_pct(start: Decimal, end: Decimal) -> Decimal | None:
    if start == 0:
        return None
    return ((end - start) / start * Decimal("100")).quantize(
        _DISPLAY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _period_share_delta(
    brand_start: Decimal,
    brand_end: Decimal,
    market_start: Decimal | None,
    market_end: Decimal | None,
) -> Decimal | None:
    if market_start is None or market_end is None:
        return None
    display_market_start = _display_decimal(market_start)
    display_market_end = _display_decimal(market_end)
    if display_market_start == 0 or display_market_end == 0:
        return None
    share_start = (brand_start / display_market_start * Decimal("100")).quantize(
        _DISPLAY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    share_end = (brand_end / display_market_end * Decimal("100")).quantize(
        _DISPLAY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return _display_decimal(share_end - share_start)


def _fixed_display(value: Decimal, suffix: str, *, signed: bool = False) -> str:
    quantized = _display_decimal(value)
    prefix = "+" if signed and quantized > 0 else ""
    return f"{prefix}{format(quantized, '.2f')}{suffix}"


def _decimal_value(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _walk_values(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        return tuple(item for nested in value.values() for item in _walk_values(nested))
    if isinstance(value, (list, tuple)):
        return tuple(item for nested in value for item in _walk_values(nested))
    return (value,)


def _number_tokens(value: str) -> tuple[str, ...]:
    import re

    return tuple(
        match.group(0).replace(",", "").lstrip("+")
        for match in re.finditer(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?", value)
    )


def _topic_particle(subject: str) -> str:
    code = ord(subject[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"
