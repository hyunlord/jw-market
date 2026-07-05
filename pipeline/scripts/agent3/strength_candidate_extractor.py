from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .json_util import parse_history, parse_json_object


IQVIA_DIMENSION_LABELS: dict[str, str] = {
    "strength": "용량",
    "strength_pack": "성분용량",
    "pack_desc": "포장",
    "form": "제형",
    "form_desc": "제형",
    "nhi_type": "급여",
    "audit_code": "audit_code",
}


@dataclass(frozen=True, slots=True)
class CandidateFloors:
    min_delta_abs: float = 50_000_000.0
    min_delta_pct: float = 5.0
    min_recent_value: float = 100_000_000.0
    min_contribution_pct: float = 1.0
    # Low-base guard: if a slice baseline is below 1% of the brand/source
    # recent total, large percentage jumps are volatile rather than a stable
    # strength signal.
    low_base_baseline_contribution_pct: float = 1.0
    low_base_score_multiplier: float = 0.5


@dataclass(frozen=True, slots=True)
class MetricRow:
    brand_name: str
    brand_key: str
    source: str
    measure: str
    raw_value_history: dict[str, float] = field(default_factory=dict)
    channel_data: dict[str, Any] = field(default_factory=dict)
    specialty_data: dict[str, Any] = field(default_factory=dict)
    channel_specialty_matrix: dict[str, Any] = field(default_factory=dict)
    dimension_data: dict[str, Any] = field(default_factory=dict)


def extract_strength_candidates(
    rows: list[MetricRow],
    *,
    floors: CandidateFloors = CandidateFloors(),
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Extract deterministic Agent3 strength candidates from mart slice histories."""

    candidates: list[dict[str, Any]] = []
    for row in rows:
        total_history = _normalize_history(row.raw_value_history)
        total_recent = _latest_value(total_history)
        for slice_name, evidence, history in _iter_slices(row):
            candidate = _candidate_from_history(
                row=row,
                slice_name=slice_name,
                evidence=evidence,
                history=history,
                total_recent=total_recent,
                floors=floors,
            )
            if candidate is not None:
                candidates.append(candidate)
    deduped = _deduplicate_identical_values(candidates)
    return sorted(deduped, key=lambda item: (-float(item["candidate_score"]), item["slice"]))[:top_n]


def _iter_slices(row: MetricRow) -> list[tuple[str, str, dict[str, float]]]:
    slices: list[tuple[str, str, dict[str, float]]] = []
    source = row.source.lower()
    source_label = "UBIST" if source == "ubist" else "IQVIA"
    if row.raw_value_history:
        slices.append((f"전체 {source_label}", "raw_value_history", _normalize_history(row.raw_value_history)))
    if source == "ubist":
        for facility, history in parse_json_object(row.channel_data).items():
            slices.append((f"UBIST 종별: {facility}", f"channel_data.{facility}", parse_history(history)))
        for specialty, history in parse_json_object(row.specialty_data).items():
            slices.append((f"UBIST 진료과: {specialty}", f"specialty_data.{specialty}", parse_history(history)))
        for facility, specialties in parse_json_object(row.channel_specialty_matrix).items():
            if not isinstance(specialties, dict):
                continue
            for specialty, history in specialties.items():
                slices.append(
                    (
                        f"UBIST 종별×진료과: {facility} / {specialty}",
                        f"channel_specialty_matrix.{facility}.{specialty}",
                        parse_history(history),
                    )
                )
    elif source == "iqvia_nsa":
        for dimension, members in parse_json_object(row.dimension_data).items():
            if dimension not in IQVIA_DIMENSION_LABELS or not isinstance(members, dict):
                continue
            label = IQVIA_DIMENSION_LABELS[dimension]
            for member, history in members.items():
                slices.append((f"IQVIA {label}: {member}", f"dimension_data.{dimension}.{member}", parse_history(history)))
    return slices


def _candidate_from_history(
    *,
    row: MetricRow,
    slice_name: str,
    evidence: str,
    history: dict[str, float],
    total_recent: float | None,
    floors: CandidateFloors,
) -> dict[str, Any] | None:
    points = [(period, value) for period, value in sorted(history.items()) if value is not None]
    if len(points) < 2:
        return None
    period, current = points[-1]
    previous_period, previous = points[-2]
    delta_abs = current - previous
    delta_pct = _pct_delta(current, previous)
    yoy_period = _yoy_period(period, history)
    yoy_value = history.get(yoy_period) if yoy_period else None
    yoy_delta_abs = current - yoy_value if yoy_value is not None else None
    yoy_delta_pct = _pct_delta(current, yoy_value) if yoy_value is not None else None
    contribution = (current / total_recent * 100) if total_recent and total_recent > 0 else None
    if not _passes_floors(current, delta_abs, delta_pct, contribution, floors):
        return None
    score = _score(current=current, delta_abs=delta_abs, delta_pct=delta_pct, contribution=contribution, yoy_delta_pct=yoy_delta_pct)
    low_base = _is_low_base(previous, total_recent, floors)
    final_score = round(score * floors.low_base_score_multiplier, 6) if low_base else score
    return {
        "brand": row.brand_name,
        "source": row.source,
        "measure": row.measure,
        "slice": slice_name,
        "period": period,
        "comparison_period": previous_period,
        "yoy_period": yoy_period,
        "metric": "recent_growth",
        "value_current": current,
        "value_baseline": previous,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "yoy_value_baseline": yoy_value,
        "yoy_delta_abs": yoy_delta_abs,
        "yoy_delta_pct": yoy_delta_pct,
        "contribution_pct": contribution,
        "candidate_score": final_score,
        "candidate_score_before_low_base_penalty": score,
        "low_base": low_base,
        "caveats": ["기저가 낮아 변동성이 큼"] if low_base else [],
        "evidence": evidence,
        "display_numbers": {
            "value_current": _display_number(current),
            "value_baseline": _display_number(previous),
            "delta_abs": _display_number(delta_abs),
            "delta_pct": _display_pct(delta_pct),
            "yoy_value_baseline": _display_number(yoy_value),
            "yoy_delta_abs": _display_number(yoy_delta_abs),
            "yoy_delta_pct": _display_pct(yoy_delta_pct),
            "contribution_pct": _display_pct(contribution),
        },
    }


def _passes_floors(
    current: float,
    delta_abs: float,
    delta_pct: float | None,
    contribution: float | None,
    floors: CandidateFloors,
) -> bool:
    return (
        current >= floors.min_recent_value
        and delta_abs >= floors.min_delta_abs
        and delta_pct is not None
        and delta_pct >= floors.min_delta_pct
        and contribution is not None
        and contribution >= floors.min_contribution_pct
    )


def _score(
    *,
    current: float,
    delta_abs: float,
    delta_pct: float | None,
    contribution: float | None,
    yoy_delta_pct: float | None,
) -> float:
    pct_component = max(delta_pct or 0.0, 0.0) * 2.0
    yoy_component = max(yoy_delta_pct or 0.0, 0.0)
    contribution_component = max(contribution or 0.0, 0.0) * 3.0
    value_component = min(current / 100_000_000.0, 50.0)
    delta_component = min(delta_abs / 10_000_000.0, 50.0)
    return round(pct_component + yoy_component + contribution_component + value_component + delta_component, 6)


def _is_low_base(baseline: float, total_recent: float | None, floors: CandidateFloors) -> bool:
    if total_recent is None or total_recent <= 0:
        return False
    return baseline / total_recent * 100 < floors.low_base_baseline_contribution_pct


def _deduplicate_identical_values(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_values: dict[tuple[float, float, float], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            float(candidate["value_current"]),
            float(candidate["value_baseline"]),
            float(candidate["delta_abs"]),
        )
        current = best_by_values.get(key)
        if current is None or _slice_specificity(candidate["slice"]) > _slice_specificity(current["slice"]):
            best_by_values[key] = candidate
    return list(best_by_values.values())


def _slice_specificity(slice_name: str) -> int:
    if "×" in slice_name:
        return 3
    if ":" in slice_name:
        return 2
    return 1


def _normalize_history(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        return parse_history(value)
    return parse_history(value)


def _latest_value(history: dict[str, float]) -> float | None:
    if not history:
        return None
    return history[max(history)]


def _pct_delta(current: float, baseline: float | None) -> float | None:
    if baseline is None or baseline == 0:
        return None
    return (current - baseline) / baseline * 100


def _yoy_period(period: str, history: dict[str, float]) -> str | None:
    if "-Q" in period:
        year_text, quarter = period.split("-Q", 1)
        candidate = f"{int(year_text) - 1}-Q{quarter}"
    else:
        year_text, month = period.split("-", 1)
        candidate = f"{int(year_text) - 1}-{month}"
    return candidate if candidate in history else None


def _display_number(value: float | None) -> str | None:
    if value is None:
        return None
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.1f}억원"
    if abs(value) >= 10_000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _display_pct(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.1f}%"
