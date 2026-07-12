from __future__ import annotations

from dataclasses import dataclass, field
import math
import statistics
from typing import Any

from .display_format import display_aliases as _display_aliases
from .display_format import display_number as _display_number
from .display_format import display_pct as _display_pct
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
    scale_max_rank: int = 5
    scale_min_latest_value: float = 500_000_000.0
    scale_min_share_pct: float = 5.0
    scale_min_market_brand_count: int = 3
    stable_min_latest_value: float = 500_000_000.0
    stable_max_cv_pct: float = 20.0
    stable_min_window_change_pct: float = -10.0


@dataclass(frozen=True, slots=True)
class MetricRow:
    brand_name: str
    brand_key: str
    source: str
    measure: str
    atc4_code: str = ""
    raw_value_history: dict[str, float] = field(default_factory=dict)
    channel_data: dict[str, Any] = field(default_factory=dict)
    specialty_data: dict[str, Any] = field(default_factory=dict)
    channel_specialty_matrix: dict[str, Any] = field(default_factory=dict)
    dimension_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketMetricRow:
    brand_key: str
    brand_name: str
    source: str
    atc4_code: str
    raw_value_history: dict[str, float] = field(default_factory=dict)
    atc4_desc: str = ""


def extract_strength_candidates(
    rows: list[MetricRow],
    *,
    market_rows: list[MarketMetricRow] | None = None,
    floors: CandidateFloors = CandidateFloors(),
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Extract deterministic Agent3 strength candidates from mart slice histories."""

    recent_candidates: list[dict[str, Any]] = []
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
                recent_candidates.append(candidate)

    deduped_recent = _deduplicate_identical_values(recent_candidates)
    ranked_recent = sorted(
        deduped_recent,
        key=lambda item: (-float(item["candidate_score"]), item["slice"]),
    )
    scale_candidates = _scale_leadership_candidates(rows, market_rows or [], floors)
    stable_candidates = _stable_core_candidates(rows, market_rows or [], floors)
    if not scale_candidates and not stable_candidates:
        return ranked_recent[:top_n]
    return [*ranked_recent[:3], *scale_candidates[:1], *stable_candidates[:1]][:top_n]


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
    display_values = {
        "value_current": current,
        "value_baseline": previous,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "yoy_value_baseline": yoy_value,
        "yoy_delta_abs": yoy_delta_abs,
        "yoy_delta_pct": yoy_delta_pct,
        "contribution_pct": contribution,
    }
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
        "display_number_aliases": {
            key: _display_aliases(key, value)
            for key, value in display_values.items()
        },
    }


def _scale_leadership_candidates(
    rows: list[MetricRow],
    market_rows: list[MarketMetricRow],
    floors: CandidateFloors,
) -> list[dict[str, Any]]:
    if not market_rows:
        return []
    market_histories = _market_histories(market_rows)
    candidates: list[dict[str, Any]] = []
    target_scopes = {
        (row.brand_key, row.source.lower(), _canonical_atc4(row.atc4_code, row.source))
        for row in rows
        if row.atc4_code
    }
    for brand_key, source, atc4_code in target_scopes:
        histories = market_histories.get((source, atc4_code))
        if not histories or brand_key not in histories:
            continue
        latest_period = max(period for history in histories.values() for period in history)
        latest_values = {key: history.get(latest_period, 0.0) for key, history in histories.items()}
        market_size = sum(latest_values.values())
        ranked_keys = sorted(
            latest_values,
            key=lambda key: (-latest_values[key], -sum(histories[key].values()), key),
        )
        rank = ranked_keys.index(brand_key) + 1
        latest_value = latest_values[brand_key]
        share_pct = latest_value / market_size * 100 if market_size > 0 else 0.0
        market_brand_count = len(latest_values)
        if not (
            rank <= floors.scale_max_rank
            and latest_value >= floors.scale_min_latest_value
            and share_pct >= floors.scale_min_share_pct
            and market_brand_count >= floors.scale_min_market_brand_count
        ):
            continue
        candidates.append(
            {
                "brand": _brand_name(rows, brand_key),
                "source": source,
                "measure": "sales",
                "slice": f"전체 {_source_label(source)} / ATC4 {atc4_code}",
                "period": latest_period,
                "metric": "scale_leadership",
                "rank": rank,
                "share_pct": share_pct,
                "market_brand_count": market_brand_count,
                "latest_value": latest_value,
                "candidate_score": round(10_000.0 - rank * 100.0 + share_pct, 6),
                "low_base": False,
                "caveats": [],
                "evidence": f"market_scope.{source}.{atc4_code}.{latest_period}",
                "display_numbers": {
                    "rank": f"{rank}위",
                    "share_pct": _display_pct(share_pct),
                    "market_brand_count": f"{market_brand_count}개",
                    "latest_value": _display_number(latest_value),
                },
                "display_number_aliases": {
                    "share_pct": _display_aliases("share_pct", share_pct),
                    "latest_value": _display_aliases("latest_value", latest_value),
                },
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            int(item["rank"]),
            -float(item["share_pct"]),
            -float(item["latest_value"]),
            item["slice"],
        ),
    )


def _stable_core_candidates(
    rows: list[MetricRow],
    market_rows: list[MarketMetricRow],
    floors: CandidateFloors,
) -> list[dict[str, Any]]:
    latest_by_scope = _latest_periods_by_scope(market_rows)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        history = _normalize_history(row.raw_value_history)
        source = row.source.lower()
        atc4_code = _canonical_atc4(row.atc4_code, source)
        latest_period = latest_by_scope.get((source, atc4_code)) if atc4_code else None
        candidate = _stable_core_candidate(
            row=row,
            history=history,
            latest_period=latest_period or (max(history) if history else None),
            floors=floors,
        )
        if candidate is not None:
            candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (float(item["cv_pct"]), -float(item["latest_value"]), item["slice"]),
    )


def _stable_core_candidate(
    *,
    row: MetricRow,
    history: dict[str, float],
    latest_period: str | None,
    floors: CandidateFloors,
) -> dict[str, Any] | None:
    if latest_period is None:
        return None
    source = row.source.lower()
    observation_count = 12 if source == "ubist" else 4
    indexed = sorted(
        (index, period, value)
        for period, value in history.items()
        if (index := _period_index(period, source)) is not None
    )
    latest_index = _period_index(latest_period, source)
    if latest_index is None:
        return None
    window = [item for item in indexed if item[0] <= latest_index][-observation_count:]
    if not (
        len(window) == observation_count
        and window[-1][1] == latest_period
        and all(window[index][0] + 1 == window[index + 1][0] for index in range(len(window) - 1))
        and all(value > 0 for _, _, value in window)
    ):
        return None
    values = [value for _, _, value in window]
    mean = statistics.fmean(values)
    cv_pct = statistics.pstdev(values) / mean * 100 if mean > 0 else math.inf
    window_change_pct = _pct_delta(values[-1], values[0])
    latest_value = values[-1]
    if not (
        latest_value >= floors.stable_min_latest_value
        and cv_pct <= floors.stable_max_cv_pct
        and window_change_pct is not None
        and window_change_pct >= floors.stable_min_window_change_pct
    ):
        return None
    atc4_code = _canonical_atc4(row.atc4_code, source)
    slice_name = f"전체 {_source_label(source)}"
    if atc4_code:
        slice_name += f" / ATC4 {atc4_code}"
    return {
        "brand": row.brand_name,
        "source": row.source,
        "measure": row.measure,
        "slice": slice_name,
        "period": latest_period,
        "comparison_period": window[0][1],
        "metric": "stable_core",
        "cv_pct": cv_pct,
        "window_change_pct": window_change_pct,
        "observation_count": observation_count,
        "latest_value": latest_value,
        "candidate_score": round(10_000.0 - cv_pct + max(window_change_pct, -100.0) / 100.0, 6),
        "low_base": False,
        "caveats": [],
        "evidence": f"raw_value_history.{window[0][1]}..{latest_period}",
        "display_numbers": {
            "cv_pct": _display_pct(cv_pct),
            "window_change_pct": _display_pct(window_change_pct),
            "observation_count": f"{observation_count}{'개월' if source == 'ubist' else '분기'}",
            "latest_value": _display_number(latest_value),
        },
        "display_number_aliases": {
            "cv_pct": _display_aliases("cv_pct", cv_pct),
            "window_change_pct": _display_aliases("window_change_pct", window_change_pct),
            "latest_value": _display_aliases("latest_value", latest_value),
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
    best_by_values: dict[tuple[str, float, float, float], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            str(candidate["metric"]),
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


def _market_histories(
    rows: list[MarketMetricRow],
) -> dict[tuple[str, str], dict[str, dict[str, float]]]:
    markets: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for row in rows:
        source = row.source.lower()
        atc4_code = _canonical_atc4(row.atc4_code, source)
        if not atc4_code:
            continue
        history = markets.setdefault((source, atc4_code), {}).setdefault(row.brand_key, {})
        for period, value in _normalize_history(row.raw_value_history).items():
            history[period] = history.get(period, 0.0) + value
    return markets


def _latest_periods_by_scope(rows: list[MarketMetricRow]) -> dict[tuple[str, str], str]:
    latest: dict[tuple[str, str], str] = {}
    for source_atc4, histories in _market_histories(rows).items():
        periods = [period for history in histories.values() for period in history]
        if periods:
            latest[source_atc4] = max(periods)
    return latest


def _canonical_atc4(value: str, source: str) -> str:
    code = value.strip().upper()
    if source.lower() != "ubist":
        return code
    if len(code) == 4 and code[0].isalpha() and code[1].isdigit():
        return code[0] + "0" + code[1:]
    if len(code) == 3 and code[0].isalpha() and code[1].isdigit() and code[2].isalpha():
        return code[0] + "0" + code[1:] + "0"
    return code


def _period_index(period: str, source: str) -> int | None:
    try:
        if source == "ubist":
            year, month = period.split("-", 1)
            return int(year) * 12 + int(month)
        year, quarter = period.split("-Q", 1)
        return int(year) * 4 + int(quarter)
    except (TypeError, ValueError):
        return None


def _source_label(source: str) -> str:
    return "UBIST" if source == "ubist" else "IQVIA"


def _brand_name(rows: list[MetricRow], brand_key: str) -> str:
    return next((row.brand_name for row in rows if row.brand_key == brand_key), brand_key)
