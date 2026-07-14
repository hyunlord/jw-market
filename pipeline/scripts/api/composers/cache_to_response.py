from __future__ import annotations

from typing import Any

from pipeline.scripts.api.composers.number_format import DISPLAY_KEY_SUFFIXES, format_number
from pipeline.scripts.api.market_growth import fixed_five_year_growth_series, growth_endpoint_meta
from pipeline.scripts.api.utils import loads_json_maybe


MEASURE_TO_SERIES_KEY = {
    "sales": "value_series",
    "volume": "volume_series",
    "unit": "unit_series",
    "dosage_unit": "dosage_unit_series",
    "counting_unit": "counting_unit_series",
}
ALL_SERIES_KEYS = set(MEASURE_TO_SERIES_KEY.values())


def _series_dict_to_points(
    value: dict,
    *,
    value_key: str,
    source: str | None = None,
) -> list[dict[str, Any]]:
    def numeric_value(item: object) -> float | None:
        if isinstance(item, dict):
            candidate = item.get("value", item.get("raw_value", item.get("market_size")))
        else:
            candidate = item
        if isinstance(candidate, bool) or not isinstance(candidate, int | float):
            return None
        return float(candidate)

    numeric_values = {
        str(period): numeric_value(item)
        for period, item in value.items()
    }
    growth_by_period = fixed_five_year_growth_series(numeric_values, source=source) if value_key == "value" else {}
    points: list[dict[str, Any]] = []
    for period in sorted(value.keys()):
        item = value[period]
        if isinstance(item, dict):
            point = {"period": period, **item}
            if value_key not in point:
                for candidate in ("value", "raw_value", "market_size", "hhi"):
                    if candidate in item:
                        point[value_key] = item[candidate]
                        break
        else:
            point = {"period": period, value_key: item}
        if value_key == "value" and "sales_krw" not in point:
            point["sales_krw"] = point.get("value")
        if value_key == "value":
            point["mom_growth_pct"] = growth_by_period[str(period)].value
        points.append(point)
    return points


def _frontend_shape_aliases(key: str, value: Any, source: str | None) -> Any:
    if key == "market_size_series" and isinstance(value, dict):
        return _series_dict_to_points(value, value_key="value", source=source)
    if key == "hhi_series_5y" and isinstance(value, dict):
        return _series_dict_to_points(value, value_key="hhi")
    if key in {"ei_ms_matrix", "growth_contribution_ms_matrix"} and isinstance(value, list):
        shares = [
            item.get("share_pct", item.get("ms", item.get("ms_recent_pct")))
            for item in value
            if isinstance(item, dict)
        ]
        numeric_shares = [share for share in shares if isinstance(share, int | float)]
        avg_share = sum(numeric_shares) / len(numeric_shares) if numeric_shares else 0.0
        return {"data": value, "ms_avg_pct": avg_share, "share_avg_pct": avg_share}
    return value


def _anomaly_aliases(obj: dict[str, Any]) -> dict[str, Any]:
    if "yoy_pct" in obj and "delta_pct" not in obj:
        obj["delta_pct"] = obj.get("yoy_pct")
    if "z_score" not in obj and ("delta_pct" in obj or "yoy_pct" in obj):
        obj["z_score"] = 0.0
    if "fallback_rank" not in obj and ("delta_pct" in obj or "yoy_pct" in obj):
        obj["fallback_rank"] = None
    if "matched_event_id" not in obj and ("delta_pct" in obj or "yoy_pct" in obj):
        obj["matched_event_id"] = None
    return obj


def _frontend_entry_aliases(obj: dict[str, Any]) -> dict[str, Any]:
    """Add non-spec frontend compatibility aliases without changing cache shape.

    v3.4 is the original mockup wired to the real backend. A few render paths
    still read old matrix/KPI field names, so expose aliases at response time
    while keeping the persisted v0.9.1 cache payload intact.
    """
    if "ms" in obj and "ms_recent_pct" not in obj:
        obj["ms_recent_pct"] = obj.get("ms")
    if "ms" in obj and "share_pct" not in obj:
        obj["share_pct"] = obj.get("ms")
    if "ei_5y" in obj and "ei" not in obj:
        obj["ei"] = obj.get("ei_5y")
    if "target_ei" in obj and "ei" not in obj:
        obj["ei"] = obj.get("target_ei")
    if "target_momentum" in obj and "momentum_score" not in obj:
        obj["momentum_score"] = obj.get("target_momentum")
    return obj


def _clean_dict_recursive(obj: Any, measure: str | None = None, source: str | None = None) -> Any:
    if isinstance(obj, list):
        return [_clean_dict_recursive(item, measure, source) for item in obj]
    if not isinstance(obj, dict):
        return format_number(obj)

    source_key = MEASURE_TO_SERIES_KEY.get(measure or "")
    if source_key and any(key in obj for key in ALL_SERIES_KEYS):
        picked = obj.get(source_key, obj.get("value_series", []))
        cleaned = {
            key: _clean_dict_recursive(_frontend_shape_aliases(key, value, source), measure, source)
            for key, value in obj.items()
            if key not in ALL_SERIES_KEYS and not str(key).endswith(DISPLAY_KEY_SUFFIXES)
        }
        cleaned["value_series"] = _clean_dict_recursive(picked, measure, source)
        return _frontend_entry_aliases(_anomaly_aliases(cleaned))

    cleaned = {
        key: _clean_dict_recursive(_frontend_shape_aliases(key, value, source), measure, source)
        for key, value in obj.items()
        if not str(key).endswith(DISPLAY_KEY_SUFFIXES)
    }
    market_meta = cleaned.get("market_meta")
    market_series = cleaned.get("market_size_series")
    data = cleaned.get("data")
    if not isinstance(market_series, list) and isinstance(data, dict):
        market_series = data.get("market_size_series")
    if isinstance(market_meta, dict) and isinstance(market_series, list):
        values = {
            str(point.get("period")): point.get("value")
            for point in market_series
            if isinstance(point, dict) and point.get("period") is not None
        }
        market_meta["mom_growth_meta"] = growth_endpoint_meta(values)
    return _frontend_entry_aliases(_anomaly_aliases(cleaned))


def compose_cached_json(raw: Any, measure: str | None = None, source: str | None = None) -> Any:
    return _clean_dict_recursive(loads_json_maybe(raw), measure, source)
