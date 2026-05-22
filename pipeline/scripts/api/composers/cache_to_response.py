from __future__ import annotations

from typing import Any

from pipeline.scripts.api.composers.number_format import deep_format_numbers
from pipeline.scripts.api.utils import loads_json_maybe


MEASURE_TO_SERIES_KEY = {
    "sales": "value_series",
    "volume": "volume_series",
    "unit": "unit_series",
    "dosage_unit": "dosage_unit_series",
    "counting_unit": "counting_unit_series",
}
ALL_SERIES_KEYS = set(MEASURE_TO_SERIES_KEY.values())


def _clean_dict_recursive(obj: Any, measure: str | None = None) -> Any:
    if isinstance(obj, list):
        return [_clean_dict_recursive(item, measure) for item in obj]
    if not isinstance(obj, dict):
        return obj

    source_key = MEASURE_TO_SERIES_KEY.get(measure or "")
    if source_key and any(key in obj for key in ALL_SERIES_KEYS):
        picked = obj.get(source_key, obj.get("value_series", []))
        cleaned = {
            key: _clean_dict_recursive(value, measure)
            for key, value in obj.items()
            if key not in ALL_SERIES_KEYS
        }
        cleaned["value_series"] = _clean_dict_recursive(picked, measure)
        return cleaned

    return {key: _clean_dict_recursive(value, measure) for key, value in obj.items()}


def compose_cached_json(raw: Any, measure: str | None = None) -> Any:
    return deep_format_numbers(_clean_dict_recursive(loads_json_maybe(raw), measure))
