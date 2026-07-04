from __future__ import annotations

from typing import Any, Mapping

from jw_chat_agent_poc.tools.query_layer.render import level_segments, metric_name, source_label
from jw_chat_agent_poc.tools.query_layer.spec import as_list, dimension_value
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


def metric_render_data(snapshot: MartSnapshot, market: str, source: str, record: MartRecord, metric: str, period: str) -> dict[str, Any]:
    value = snapshot.value(record, period)
    market_value = snapshot.market_value(market, period, source)
    series_periods = snapshot.periods(market, source)[-10:]
    return {
        "brand": record.brand_name,
        "metric": metric_name(metric),
        "period": period,
        "market_id": market,
        "market_name": market,
        "source_label": source_label(source),
        "sales_krw": value,
        "sales_억원": round(value / 100_000_000, 2),
        "ms_recent_pct": snapshot.share(market, record, period, source),
        "rank": snapshot.rank(market, record.brand_name, period, source),
        "total_brands_in_market": len(snapshot.market_records(market, source)),
        "market_size_recent_krw": market_value,
        "market_size_억원": round(market_value / 100_000_000, 2),
        "hhi_recent": round(snapshot.hhi(market, period, source), 4),
        "brand_value_series_10pt": snapshot.brand_series(market, record.brand_name, series_periods, source),
        "market_size_series": snapshot.market_series(market, series_periods, source),
        "level": "Brand",
        "level_segments": level_segments(snapshot.ranked_brands(market, period, source)[:10]),
        "level_top5_trend_series": top_trend(snapshot, market, source, period, record.brand_name),
    }


def top_trend(snapshot: MartSnapshot, market: str, source: str, period: str, anchor_brand: str, limit: int = 5) -> list[dict[str, Any]]:
    periods = snapshot.periods(market, source)[-10:]
    ranked = snapshot.ranked_brands(market, period, source)
    selected = [row["brand"] for row in ranked[:limit]]
    if anchor_brand not in selected:
        selected.append(anchor_brand)
    out: list[dict[str, Any]] = []
    for brand in selected:
        if not brand:
            continue
        record = snapshot.record(market, brand, source)
        series = snapshot.brand_series(market, brand, periods, source)
        latest = series[-1]
        first = series[0]
        out.append(
            {
                "brand": brand,
                "rank": latest.get("rank"),
                "ms_recent_pct": latest.get("ms_pct"),
                "share_delta_pctp": round(float(latest.get("ms_pct") or 0) - float(first.get("ms_pct") or 0), 4),
                "value_recent": latest.get("value_krw"),
                "value_recent_억원": latest.get("value_억원"),
                "value_delta_krw": float(latest.get("value_krw") or 0) - float(first.get("value_krw") or 0),
                "series": series,
                "company": record.company(),
            }
        )
    return out


def grouped_rows(snapshot: MartSnapshot, market: str, source: str, spec: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    group_by = as_list(spec.get("group_by")) or as_list(spec.get("dimensions")) or ["product"]
    key = group_by[0]
    period = _period_from_filters(snapshot, market, source, spec)
    filters = _filters(spec)
    if key in {"product", "brand"} and not filters:
        return snapshot.ranked_brands(market, period, source)[:limit]
    rows = _ranked_group_rows(snapshot, market, source, key, period, filters)
    return rows[:limit]


def grouped_trends(snapshot: MartSnapshot, market: str, source: str, spec: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    group_by = as_list(spec.get("group_by")) or as_list(spec.get("dimensions")) or ["product"]
    key = next((item for item in group_by if item != "period"), group_by[0])
    filters = _filters(spec)
    periods = _trend_periods(snapshot, market, source, spec)
    latest_rows = _ranked_group_rows(snapshot, market, source, key, periods[-1], filters)[:limit]
    selected = [str(row["name"]) for row in latest_rows]
    out: list[dict[str, Any]] = []
    for label in selected:
        series = _series_for_group(snapshot, market, source, key, label, periods, filters)
        if not series:
            continue
        first = series[0]
        latest = series[-1]
        out.append(
            {
                "brand": label,
                "name": label,
                "rank": latest.get("rank"),
                "ms_recent_pct": latest.get("ms_pct"),
                "share_delta_pctp": round(float(latest.get("ms_pct") or 0) - float(first.get("ms_pct") or 0), 4),
                "value_recent": latest.get("value_krw"),
                "value_recent_억원": latest.get("value_억원"),
                "value_delta_krw": float(latest.get("value_krw") or 0) - float(first.get("value_krw") or 0),
                "value_delta_억원": round((float(latest.get("value_krw") or 0) - float(first.get("value_krw") or 0)) / 100_000_000, 2),
                "series": series,
            }
        )
    return out


def brand_yoy_data(snapshot: MartSnapshot, market: str, source: str, brand: str) -> dict[str, Any]:
    latest = snapshot.latest_period(market, source)
    prior = f"{int(latest[:4]) - 1}{latest[4:]}" if len(latest) == 7 else ""
    record = snapshot.record(market, brand, source)
    current = snapshot.value(record, latest)
    base = snapshot.value(record, prior) if prior else 0.0
    growth = (current / base - 1) * 100 if base else 0.0
    return {
        "brand": brand,
        "metric": "yoy_growth",
        "period": f"{prior}→{latest}" if prior else latest,
        "from_period": prior,
        "to_period": latest,
        "from_sales_krw": base,
        "from_sales_억원": round(base / 100_000_000, 2),
        "to_sales_krw": current,
        "to_sales_억원": round(current / 100_000_000, 2),
        "sales_delta_krw": current - base,
        "sales_delta_억원": round((current - base) / 100_000_000, 2),
        "growth_pct": round(growth, 4),
        "brand_value_series_10pt": snapshot.brand_series(market, brand, (prior, latest), source) if prior else snapshot.brand_series(market, brand, (latest,), source),
    }


def brand_average_share_data(snapshot: MartSnapshot, market: str, source: str, brand: str, count: int) -> dict[str, Any]:
    periods = snapshot.periods(market, source)[-max(1, min(count, 24)) :]
    series = snapshot.brand_series(market, brand, periods, source)
    shares = [float(item.get("ms_pct") or 0.0) for item in series]
    avg = sum(shares) / len(shares) if shares else 0.0
    return {
        "brand": brand,
        "metric": "average_share",
        "period": f"{periods[0]}→{periods[-1]}" if periods else "latest",
        "avg_ms_pct": round(avg, 4),
        "brand_value_series_10pt": series,
    }


def _ranked_group_rows(snapshot: MartSnapshot, market: str, source: str, key: str, period: str, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped = _group_values(snapshot, market, source, key, period, filters)
    rows = []
    for name, value in grouped.items():
        denominator = _share_denominator(snapshot, market, source, key, period, filters, name, grouped)
        rows.append({"brand": name, "name": name, "value": value, "ms_recent_pct": value / denominator * 100 if denominator else 0.0})
    rows.sort(key=lambda item: item["value"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def _group_values(snapshot: MartSnapshot, market: str, source: str, key: str, period: str, filters: Mapping[str, Any]) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for record in snapshot.market_records(market, source):
        if not _record_matches(record, filters):
            continue
        for label, value in _record_group_values(snapshot, record, key, period, filters):
            grouped[label] = grouped.get(label, 0.0) + value
    return grouped


def _record_group_values(snapshot: MartSnapshot, record: MartRecord, key: str, period: str, filters: Mapping[str, Any]) -> list[tuple[str, float]]:
    if key == "channel":
        return [(label, _period_value(history, period)) for label, history in _selected_nested(record.channel_data, "channel", filters)]
    if key == "specialty":
        return [(label, _period_value(history, period)) for label, history in _selected_nested(record.specialty_data, "specialty", filters)]
    label = dimension_value(record, key)
    return [(label, _record_value(snapshot, record, period, filters))]


def _record_value(snapshot: MartSnapshot, record: MartRecord, period: str, filters: Mapping[str, Any]) -> float:
    channel = str(filters.get("channel") or "")
    if channel:
        return _period_value(_nested(record.channel_data, channel), period)
    specialty = str(filters.get("specialty") or "")
    if specialty:
        return _period_value(_nested(record.specialty_data, specialty), period)
    return snapshot.value(record, period)


def _share_denominator(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    key: str,
    period: str,
    filters: Mapping[str, Any],
    label: str,
    grouped: Mapping[str, float],
) -> float:
    if key == "channel" and filters.get("brand"):
        return sum(_period_value(_nested(record.channel_data, label), period) for record in snapshot.market_records(market, source))
    if key == "specialty" and filters.get("brand"):
        return sum(_period_value(_nested(record.specialty_data, label), period) for record in snapshot.market_records(market, source))
    if filters.get("channel"):
        channel = str(filters["channel"])
        return sum(_period_value(_nested(record.channel_data, channel), period) for record in snapshot.market_records(market, source))
    if filters.get("specialty"):
        specialty = str(filters["specialty"])
        return sum(_period_value(_nested(record.specialty_data, specialty), period) for record in snapshot.market_records(market, source))
    return sum(grouped.values())


def _series_for_group(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    key: str,
    label: str,
    periods: tuple[str, ...],
    filters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for period in periods:
        rows = _ranked_group_rows(snapshot, market, source, key, period, filters)
        row = next((item for item in rows if item["name"] == label), None)
        if row is None:
            continue
        value = float(row.get("value") or 0.0)
        out.append({"period": period, "value_krw": value, "value_억원": round(value / 100_000_000, 2), "ms_pct": row.get("ms_recent_pct"), "rank": row.get("rank")})
    return out


def _period_from_filters(snapshot: MartSnapshot, market: str, source: str, spec: Mapping[str, Any]) -> str:
    filters = _filters(spec)
    period = str(filters.get("period") or "")
    return period if period else snapshot.latest_period(market, source)


def _trend_periods(snapshot: MartSnapshot, market: str, source: str, spec: Mapping[str, Any]) -> tuple[str, ...]:
    filters = _filters(spec)
    count = _int(filters.get("periods"), 12)
    periods = snapshot.periods(market, source)
    return periods[-max(1, min(count, len(periods))):]


def _selected_nested(data: Mapping[str, Any], key: str, filters: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    selected = str(filters.get(key) or "")
    if selected:
        value = data.get(selected)
        return [(selected, value)] if isinstance(value, Mapping) else []
    return [(str(name), value) for name, value in data.items() if isinstance(value, Mapping)]


def _record_matches(record: MartRecord, filters: Mapping[str, Any]) -> bool:
    brand = str(filters.get("brand") or "")
    if brand and record.brand_name != brand:
        return False
    for key in ("company", "molecule", "dosage_form", "nhi_type", "ox_gx"):
        expected = str(filters.get(key) or "")
        if expected and dimension_value(record, key) != expected:
            return False
    return True


def _filters(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    filters = spec.get("filters")
    return filters if isinstance(filters, Mapping) else {}


def _nested(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _period_value(history: Mapping[str, Any], period: str) -> float:
    row = history.get(period)
    if isinstance(row, Mapping):
        value = row.get("raw_value")
        return float(value) if isinstance(value, int | float) else 0.0
    return 0.0


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
