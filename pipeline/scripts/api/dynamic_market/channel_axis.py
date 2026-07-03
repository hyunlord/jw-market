"""Runtime UBIST channel-axis slicing helpers.

``analysis_level`` filters choose market member rows.  Channel-axis filters are
different: they slice each surviving brand's facility x specialty raw matrix
and rebuild the monthly series from that selected value surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


ChannelMatrix = dict[str, dict[str, dict[str, float]]]


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

    @property
    def is_active(self) -> bool:
        return bool(self.facilities or self.specialties or self.pairs)


def parse_channel_specialty_matrix(raw: Any) -> ChannelMatrix:
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
