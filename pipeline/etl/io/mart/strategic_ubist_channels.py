from __future__ import annotations

from typing import Any

from .ubist_channel_mapping import parse_channel_code, raw_pair_to_channel_code


UBIST_CHANNEL_BY_DISPLAY_COLUMN = "ubist_channel_by_display"
UBIST_CHANNEL_BY_CODE_COLUMN = "ubist_channel_by_code"
UBIST_CHANNEL_CONTRACT_COLUMNS = (
    UBIST_CHANNEL_BY_DISPLAY_COLUMN,
    UBIST_CHANNEL_BY_CODE_COLUMN,
)


def build_ubist_channel_totals(channel_specialty_matrix: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    """Convert general mart UBIST matrix into the resolver's parquet-era contract.

    The general mart already stores the verified raw UBIST facility-specialty
    matrix as ``facility -> specialty -> period -> value``. Strategic mart
    rows persist this resolved shape so runtime code can return the same tuple
    that ``_load_market_raw_totals`` used to build from parquet:
    ``by_display[display_channel][period]`` for row series and
    ``by_code[channel_code][period]`` for fallback ranking totals.
    """
    by_display: dict[str, dict[str, float]] = {}
    by_code: dict[str, dict[str, float]] = {}
    for facility_raw, specialties in channel_specialty_matrix.items():
        if not isinstance(specialties, dict):
            continue
        for specialty_raw, series in specialties.items():
            code = raw_pair_to_channel_code(facility_raw, specialty_raw)
            if not code:
                continue
            parsed = parse_channel_code(code)
            if parsed is None or not isinstance(series, dict):
                continue
            for period, raw_value in series.items():
                numeric = _series_value(raw_value)
                if numeric <= 0.0:
                    continue
                period_text = str(period)
                display_bucket = by_display.setdefault(parsed.display_name, {})
                code_bucket = by_code.setdefault(parsed.code, {})
                display_bucket[period_text] = display_bucket.get(period_text, 0.0) + numeric
                code_bucket[period_text] = code_bucket.get(period_text, 0.0) + numeric
    return {
        "by_display": _sort_nested_series(by_display),
        "by_code": _sort_nested_series(by_code),
    }


def attach_ubist_channel_totals(row: dict[str, Any]) -> None:
    """Attach strategic UBIST channel contract columns derived from general mart."""
    if str(row.get("source") or "").lower() != "ubist":
        row[UBIST_CHANNEL_BY_DISPLAY_COLUMN] = {}
        row[UBIST_CHANNEL_BY_CODE_COLUMN] = {}
        return
    totals = build_ubist_channel_totals(row.get("channel_specialty_matrix") or {})
    row[UBIST_CHANNEL_BY_DISPLAY_COLUMN] = totals["by_display"]
    row[UBIST_CHANNEL_BY_CODE_COLUMN] = totals["by_code"]


def _series_value(value: Any) -> float:
    raw = value.get("raw_value", value.get("value", 0.0)) if isinstance(value, dict) else value
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _sort_nested_series(payload: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    return {
        channel: dict(sorted(series.items()))
        for channel, series in sorted(payload.items())
    }
