"""Runtime source-specific channel-axis slicing helpers.

``analysis_level`` filters choose market member rows.  Channel-axis filters are
different: they slice each surviving brand's raw value matrix and rebuild the
metric series from that selected value surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


ChannelMatrix = dict[str, dict[str, dict[str, float]]]
AuditCodeMatrix = dict[str, dict[str, float]]


@dataclass(frozen=True, slots=True)
class ChannelAxisPair:
    """One raw UBIST facility-specialty pair selected by the caller."""

    facility: str
    specialty: str


@dataclass(frozen=True, slots=True)
class ChannelAxisFilter:
    """Normalized channel-axis selections for one source."""

    source: str
    facilities: tuple[str, ...] = ()
    specialties: tuple[str, ...] = ()
    pairs: tuple[ChannelAxisPair, ...] = ()
    audit_codes: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        return bool(self.facilities or self.specialties or self.pairs or self.audit_codes)


def parse_channel_specialty_matrix(
    raw: Any,
    *,
    period_start: str | None = None,
    period_end: str | None = None,
) -> ChannelMatrix:
    """Parse raw UBIST facility-specialty-period matrix from the general mart."""

    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
    else:
        return {}
    if not isinstance(payload, dict):
        return {}
    parsed: ChannelMatrix = {}
    for facility, specialties in payload.items():
        if not isinstance(specialties, dict):
            continue
        facility_bucket: dict[str, dict[str, float]] = {}
        for specialty, series in specialties.items():
            if not isinstance(series, dict):
                continue
            facility_bucket[str(specialty)] = {
                str(period): float(value or 0.0)
                for period, value in series.items()
                if (period_start is None or str(period) >= period_start)
                and (period_end is None or str(period) <= period_end)
            }
        if facility_bucket:
            parsed[str(facility)] = facility_bucket
    return parsed


def slice_channel_specialty_matrix(matrix: ChannelMatrix, channel_axis: ChannelAxisFilter | None) -> ChannelMatrix:
    """Return only cells selected by the UBIST channel-axis filter."""

    if channel_axis is None or not channel_axis.is_active or channel_axis.source != "ubist":
        return matrix
    pair_keys = {(item.facility, item.specialty) for item in channel_axis.pairs}
    facility_values = set(channel_axis.facilities)
    specialty_values = set(channel_axis.specialties)
    sliced: ChannelMatrix = {}
    for facility, specialties in matrix.items():
        for specialty, series in specialties.items():
            if not _cell_selected(
                facility=facility,
                specialty=specialty,
                pair_keys=pair_keys,
                facility_values=facility_values,
                specialty_values=specialty_values,
            ):
                continue
            sliced.setdefault(facility, {})[specialty] = dict(series)
    return sliced


def history_from_channel_specialty_matrix(matrix: ChannelMatrix) -> dict[str, float]:
    """Collapse selected facility-specialty cells into period totals."""

    history: dict[str, float] = {}
    for specialties in matrix.values():
        for series in specialties.values():
            for period, value in series.items():
                history[period] = history.get(period, 0.0) + float(value or 0.0)
    return history


def parse_audit_code_matrix(raw: Any) -> AuditCodeMatrix:
    """Parse raw IQVIA audit-code-period matrix from the general mart."""

    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
    else:
        return {}
    if not isinstance(payload, dict):
        return {}
    parsed: AuditCodeMatrix = {}
    for audit_code, series in payload.items():
        if not isinstance(series, dict):
            continue
        code = str(audit_code).strip()
        if not code:
            continue
        parsed[code] = {str(period): float(value or 0.0) for period, value in series.items()}
    return parsed


def slice_audit_code_matrix(matrix: AuditCodeMatrix, channel_axis: ChannelAxisFilter | None) -> AuditCodeMatrix:
    """Return only audit codes selected by the IQVIA channel-axis filter."""

    if channel_axis is None or not channel_axis.is_active or channel_axis.source != "iqvia_nsa":
        return matrix
    selected_codes = set(channel_axis.audit_codes)
    if not selected_codes:
        return matrix
    return {audit_code: dict(series) for audit_code, series in matrix.items() if audit_code in selected_codes}


def history_from_audit_code_matrix(matrix: AuditCodeMatrix) -> dict[str, float]:
    """Collapse selected audit-code cells into period totals."""

    history: dict[str, float] = {}
    for series in matrix.values():
        for period, value in series.items():
            history[period] = history.get(period, 0.0) + float(value or 0.0)
    return history


def _cell_selected(
    *,
    facility: str,
    specialty: str,
    pair_keys: set[tuple[str, str]],
    facility_values: set[str],
    specialty_values: set[str],
) -> bool:
    if pair_keys:
        return (facility, specialty) in pair_keys
    if facility_values and facility not in facility_values:
        return False
    if specialty_values and specialty not in specialty_values:
        return False
    return True
