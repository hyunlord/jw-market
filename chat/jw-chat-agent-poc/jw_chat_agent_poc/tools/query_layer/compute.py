from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Mapping

from jw_chat_agent_poc.tools.query_layer.render import level_segments, metric_name, source_label
from jw_chat_agent_poc.tools.query_layer.market_structure import market_structure
from jw_chat_agent_poc.tools.query_layer.spec import as_list, dimension_value
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


def metric_render_data(snapshot: MartSnapshot, market: str, source: str, record: MartRecord, metric: str, period: str) -> dict[str, Any]:
    value = snapshot.value_or_none(record, period)
    if value is None:
        raise LookupError(f"mart metric row missing or failed: market={market} source={source} brand={record.brand_name} period={period}")
    market_value = snapshot.market_value_or_none(market, period, source)
    series_periods = snapshot.periods(market, source)[-10:]
    structure = market_structure(snapshot, market, source)
    data = {
        "brand": record.brand_name,
        "metric": metric_name(metric),
        "period": period,
        "market_id": market,
        "market_name": market,
        "source_label": source_label(source),
        "sales_krw": value,
        "sales_억원": round(value / 100_000_000, 2),
        "ms_recent_pct": snapshot.share_or_none(market, record, period, source),
        "rank": snapshot.rank(market, record.brand_name, period, source),
        "total_brands_in_market": len(snapshot.market_records(market, source)),
        "source_status": snapshot.value_status(record, period),
        "market_size_recent_krw": market_value,
        "market_size_억원": round(market_value / 100_000_000, 2) if market_value is not None else None,
        "hhi_recent": round(snapshot.hhi(market, period, source), 4),
        "brand_value_series_10pt": snapshot.brand_series(market, record.brand_name, series_periods, source),
        "market_size_series": snapshot.market_series(market, series_periods, source),
        "level": "Brand",
        "level_segments": level_segments(snapshot.ranked_brands(market, period, source)[:10]),
        "level_top5_trend_series": top_trend(snapshot, market, source, period, record.brand_name),
    }
    if structure:
        data["market_structure"] = structure
    return data


def derived_metric_render_data(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    record: MartRecord,
    metric: str,
) -> dict[str, Any]:
    periods = snapshot.periods(market, source)
    if not periods:
        raise LookupError(f"mart periods missing: market={market} source={source}")
    latest = periods[-1]
    data = metric_render_data(snapshot, market, source, record, metric, latest)
    metric_key = metric.casefold()
    if metric_key == "hhi":
        series = _annual_hhi_series(snapshot, market, source)
        if not series:
            raise LookupError(f"complete annual HHI history missing: market={market} source={source}")
        data["hhi_series_5y"] = series
    elif metric_key == "momentum":
        momentum = _momentum(snapshot, market, source, record, periods)
        if momentum is None:
            raise LookupError(f"four-point momentum history missing: market={market} source={source} brand={record.brand_name}")
        data["momentum_score"] = momentum
    elif metric_key == "ei":
        endpoint = _ei_metrics(snapshot, market, source, record, periods)
        if endpoint is None:
            raise LookupError(f"3y/5y endpoint history missing: market={market} source={source} brand={record.brand_name}")
        data.update(endpoint)
    elif metric_key == "growth_contribution":
        contribution = _growth_contribution(snapshot, market, source, record, periods)
        if contribution is None:
            raise LookupError(f"year-over-year growth history missing: market={market} source={source} brand={record.brand_name}")
        data.update(contribution)
    else:
        raise LookupError(f"unsupported derived metric: {metric}")
    return data


def _annual_hhi_series(snapshot: MartSnapshot, market: str, source: str) -> list[dict[str, Any]]:
    annual: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    periods_by_year: dict[int, set[str]] = defaultdict(set)
    for record in snapshot.market_records(market, source):
        for period in snapshot.periods(market, source):
            year = _period_year(period)
            value = snapshot.value_or_none(record, period)
            if year is None or value is None:
                continue
            annual[year][record.brand_name] += value
            periods_by_year[year].add(period)
    expected = 4 if source == "iqvia_nsa" else 12
    complete = sorted(year for year, periods in periods_by_year.items() if len(periods) >= expected)[-5:]
    points: list[dict[str, Any]] = []
    for year in complete:
        values = annual[year].values()
        total = sum(values)
        if total <= 0:
            continue
        shares = [round(value / total * 100.0, 4) for value in annual[year].values()]
        hhi = sum(share**2 for share in shares)
        points.append({"period": str(year), "period_full": str(year), "year": year, "hhi": round(hhi, 4)})
    return points


def _momentum(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    record: MartRecord,
    periods: tuple[str, ...],
) -> float | None:
    shares = [snapshot.share_or_none(market, record, period, source) for period in periods[-4:]]
    if len(shares) < 4 or any(value is None or not math.isfinite(float(value)) for value in shares):
        return None
    ys = [float(value) for value in shares if value is not None]
    return (4 * sum(x * y for x, y in zip((1, 2, 3, 4), ys, strict=True)) - 10 * sum(ys)) / 20


def _ei_metrics(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    record: MartRecord,
    periods: tuple[str, ...],
) -> dict[str, Any] | None:
    latest = periods[-1]
    for years in (5, 3):
        start = _period_years_before(latest, years)
        if start not in periods:
            continue
        brand_start = snapshot.value_or_none(record, start)
        brand_end = snapshot.value_or_none(record, latest)
        market_start = snapshot.market_value_or_none(market, start, source)
        market_end = snapshot.market_value_or_none(market, latest, source)
        if not all(value is not None and value > 0 for value in (brand_start, brand_end, market_start, market_end)):
            continue
        brand_cagr = ((float(brand_end) / float(brand_start)) ** (1 / years) - 1) * 100
        market_cagr = ((float(market_end) / float(market_start)) ** (1 / years) - 1) * 100
        if market_cagr == 0:
            continue
        return {
            "ei": round(brand_cagr / market_cagr * 100, 4),
            "ei_basis": f"endpoint_{years}y",
            "ei_period_years": years,
            "brand_cagr_pct": round(brand_cagr, 4),
            "market_cagr_pct": round(market_cagr, 4),
        }
    return None


def _growth_contribution(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    record: MartRecord,
    periods: tuple[str, ...],
) -> dict[str, Any] | None:
    latest = periods[-1]
    previous = _period_years_before(latest, 1)
    if previous not in periods:
        return None
    brand_latest = snapshot.value_or_none(record, latest)
    brand_previous = snapshot.value_or_none(record, previous)
    market_latest = snapshot.market_value_or_none(market, latest, source)
    market_previous = snapshot.market_value_or_none(market, previous, source)
    if any(value is None for value in (brand_latest, brand_previous, market_latest, market_previous)):
        return None
    market_growth = float(market_latest) - float(market_previous)
    if abs(market_growth) <= 10_000:
        return None
    contribution = (float(brand_latest) - float(brand_previous)) / market_growth * 100
    return {
        "growth_contribution": round(contribution, 4),
        "growth_contribution_pct": round(contribution, 4),
        "growth_contribution_basis": "year_over_year_absolute_growth",
        "growth_contribution_period_start": previous,
        "growth_contribution_period_end": latest,
    }


def _period_year(period: str) -> int | None:
    prefix = period[:4]
    return int(prefix) if len(prefix) == 4 and prefix.isdigit() else None


def _period_years_before(period: str, years: int) -> str:
    year = _period_year(period)
    return f"{year - years}{period[4:]}" if year is not None else ""


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
        if not series:
            continue
        latest = series[-1]
        first = series[0]
        out.append(
            {
                "brand": brand,
                "rank": latest.get("rank"),
                "ms_recent_pct": latest.get("ms_pct"),
                "from_period": first.get("period"),
                "from_ms_pct": first.get("ms_pct"),
                "to_period": latest.get("period"),
                "to_ms_pct": latest.get("ms_pct"),
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
                "from_period": first.get("period"),
                "from_ms_pct": first.get("ms_pct"),
                "to_period": latest.get("period"),
                "to_ms_pct": latest.get("ms_pct"),
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
        return [
            (label, value)
            for label, history in _selected_nested(record.channel_data, "channel", filters)
            for value in (_period_value_or_none(history, period),)
            if value is not None
        ]
    if key == "specialty":
        return [
            (label, value)
            for label, history in _selected_nested(record.specialty_data, "specialty", filters)
            for value in (_period_value_or_none(history, period),)
            if value is not None
        ]
    label = dimension_value(record, key)
    value = _record_value_or_none(snapshot, record, period, filters)
    return [(label, value)] if value is not None else []


def _record_value_or_none(snapshot: MartSnapshot, record: MartRecord, period: str, filters: Mapping[str, Any]) -> float | None:
    channel = str(filters.get("channel") or "")
    if channel:
        return _period_value_or_none(_nested(record.channel_data, channel), period)
    specialty = str(filters.get("specialty") or "")
    if specialty:
        return _period_value_or_none(_nested(record.specialty_data, specialty), period)
    return snapshot.value_or_none(record, period)


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
    if key in {"product", "brand"} and filters.get("brand"):
        return snapshot.market_value(market, period, source)
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
        rank = snapshot.rank(market, label, period, source) if key in {"product", "brand"} else row.get("rank")
        out.append({"period": period, "value_krw": value, "value_억원": round(value / 100_000_000, 2), "ms_pct": row.get("ms_recent_pct"), "rank": rank})
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
    for key in ("company", "molecule", "class_1", "class_2", "dosage_form", "nhi_type", "ox_gx"):
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
    value = _period_value_or_none(history, period)
    return value if value is not None else 0.0


def _period_value_or_none(history: Mapping[str, Any], period: str) -> float | None:
    row = history.get(period)
    if isinstance(row, Mapping):
        status = str(row.get("source_status", row.get("status")) or "OK")
        if status in {"query_failed", "mapping_failed", "incomplete_split", "missing", "error"}:
            return None
        value = row.get("raw_value")
        return float(value) if isinstance(value, int | float) else None
    return None


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
