from __future__ import annotations

from typing import Any, Mapping

from pipeline.scripts.api.brand_activity_csd_shared import JsonMap, float_value, text
from pipeline.scripts.api.dynamic_market.channel_axis import (
    ChannelAxisFilter,
    history_from_audit_code_matrix,
    parse_audit_code_matrix,
    slice_audit_code_matrix,
)

IQVIA_AUDIT_SOURCE = "iqvia_nsa"


def parse_audit_code_axis(value: Mapping[str, Any] | None) -> ChannelAxisFilter | None:
    """Parse Brand Activity's IQVIA audit-code channel-axis contract.

    UBIST channel-axis payloads are intentionally ignored in Brand Activity.
    """

    if not isinstance(value, Mapping):
        return None
    raw_axis = value.get("channel_axis")
    if not isinstance(raw_axis, Mapping):
        return None
    raw_iqvia = raw_axis.get("iqvia", raw_axis)
    if not isinstance(raw_iqvia, Mapping):
        return None
    codes = _audit_codes(raw_iqvia.get("audit_code"))
    if not codes:
        return None
    return ChannelAxisFilter(source=IQVIA_AUDIT_SOURCE, audit_codes=codes)


def audit_code_axis_echo(channel_axis: ChannelAxisFilter | None) -> JsonMap:
    """Return the public echo payload for an applied IQVIA audit-code slice."""

    if channel_axis is None or not channel_axis.is_active:
        return {}
    return {"source": channel_axis.source, "audit_code": list(channel_axis.audit_codes)}


def audit_code_sales_value(row: Mapping[str, Any] | None, channel_axis: ChannelAxisFilter | None, quarter: str) -> float:
    """Return IQVIA sales for one brand under the selected audit-code slice."""

    if row is None or channel_axis is None or not channel_axis.is_active:
        return 0.0
    matrix = parse_audit_code_matrix(row.get("audit_code_matrix"))
    history = history_from_audit_code_matrix(slice_audit_code_matrix(matrix, channel_axis))
    if not history:
        return 0.0
    if "-Q" in quarter:
        year, qtext = quarter.split("-Q", 1)
        months_by_q = {
            "1": ("01", "02", "03"),
            "2": ("04", "05", "06"),
            "3": ("07", "08", "09"),
            "4": ("10", "11", "12"),
        }
        monthly_total = sum(float_value(history.get(f"{year}-{month}")) for month in months_by_q.get(qtext, ()))
        return float_value(history.get(quarter)) + monthly_total
    if quarter in history:
        return float_value(history.get(quarter))
    return sum(float_value(value) for value in history.values())


def audit_code_keys(row: Mapping[str, Any]) -> set[str]:
    """Return dynamically available audit-code keys for one brand metric row."""

    return set(parse_audit_code_matrix(row.get("audit_code_matrix")))


def _audit_codes(value: Any) -> tuple[str, ...]:
    raw_values = value if isinstance(value, list | tuple) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        candidate = text(item).strip().upper()
        if candidate and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return tuple(result)
