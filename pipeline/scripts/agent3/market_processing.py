from __future__ import annotations

from collections.abc import Iterable
import math
import re
from typing import Any

from .display_format import display_number, display_pct
from .market_position import MarketPositionResult
from .market_repository import MarketUnit, StrategicMetricRow
from .strength_candidate_extractor import (
    CandidateFloors,
    MarketMetricRow,
    MetricRow,
    extract_strength_candidates,
)


def build_strategic_inputs(
    unit: MarketUnit,
    scope_rows: list[StrategicMetricRow],
    *,
    top_n: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = next(row for row in scope_rows if row.brand_key == unit.brand_key)
    latest_period = max(
        (period for row in scope_rows for period in row.raw_value_history),
        key=_period_key,
    )
    latest_values = {
        row.brand_key: row.raw_value_history.get(latest_period, 0.0)
        for row in scope_rows
    }
    market_size = math.fsum(latest_values.values())
    ranked = _ranked_keys(scope_rows, latest_period)
    rank = ranked.index(unit.brand_key) + 1
    target_value = latest_values[unit.brand_key]
    profile = {
        "brand": unit.brand_name,
        "brand_key": unit.brand_key,
        "source": unit.source,
        "sources": [unit.source],
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "market_scope": {
            "latest_period": latest_period,
            "member_count": len(scope_rows),
            "market_size_recent": market_size,
            "target_rank": rank,
            "target_share_pct": target_value / market_size * 100 if market_size > 0 else 0.0,
            "target_value_recent": target_value,
        },
    }
    candidates = extract_strength_candidates(
        [_target_metric_row(target, unit.market_id)],
        market_rows=[_market_metric_row(row, unit.market_id) for row in scope_rows],
        floors=CandidateFloors(),
        top_n=top_n,
    )
    return profile, [_native_candidate(unit, item) for item in candidates]


def build_native_market_position(
    unit: MarketUnit,
    scope_rows: list[StrategicMetricRow],
    *,
    base_summary: dict[str, Any],
) -> MarketPositionResult:
    latest_period = max(
        (period for row in scope_rows for period in row.raw_value_history),
        key=_period_key,
    )
    target = next(row for row in scope_rows if row.brand_key == unit.brand_key)
    values = {row.brand_key: row.raw_value_history.get(latest_period, 0.0) for row in scope_rows}
    market_size = math.fsum(values.values())
    ranked = _ranked_keys(scope_rows, latest_period)
    rank = ranked.index(unit.brand_key) + 1
    latest_value = values[unit.brand_key]
    share_pct = latest_value / market_size * 100 if market_size > 0 else 0.0
    observation_count = _consecutive_positive(target.raw_value_history, unit.source)
    source_label = "UBIST" if unit.source == "ubist" else "IQVIA"
    view_label = "전략 ML" if unit.view_kind == "market_landscape" else "전략 CD"
    sales_label = "처방액" if unit.source == "ubist" else "매출액"
    slice_label = f"{source_label} {view_label} {unit.market_id}"
    narrative = (
        f"{slice_label} 시장 {len(scope_rows)}개 브랜드 중 {rank}위이며, 최신 "
        f"{latest_period} {sales_label}은 {display_number(latest_value)}"
        f"(점유율 {display_pct(share_pct)})입니다."
    )
    if observation_count > 1:
        period_unit = "개월" if unit.source == "ubist" else "분기"
        narrative += f" 최근 {observation_count}{period_unit} 연속 실적이 확인됩니다."
    candidate = {
        "brand": unit.brand_name,
        "source": unit.source,
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "measure": "sales",
        "metric": "market_position",
        "slice": slice_label,
        "period": latest_period,
        "rank": rank,
        "share_pct": share_pct,
        "market_brand_count": len(scope_rows),
        "latest_value": latest_value,
        "observation_count": observation_count,
        "market_key": f"{unit.view_kind}:{unit.market_id}:{unit.source}",
        "cumulative_value": math.fsum(target.raw_value_history.values()),
        "evidence": f"strategic_scope.{unit.view_kind}.{unit.market_id}.{unit.source}.{latest_period}",
        "low_base": latest_value <= 0,
        "caveats": [],
    }
    summary = {
        **base_summary,
        "brand": unit.brand_name,
        "source": unit.source,
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "candidate_count": 1,
        "strength_items": [
            {
                "candidate_index": 0,
                "confidence": "high",
                "metric": "market_position",
                "narrative": narrative,
                "numbers": {
                    "rank": rank,
                    "share_pct": share_pct,
                    "market_brand_count": len(scope_rows),
                    "latest_value": latest_value,
                    "observation_count": observation_count,
                    "cumulative_value": candidate["cumulative_value"],
                },
                "period": latest_period,
                "slice": slice_label,
            }
        ],
    }
    return MarketPositionResult(candidate=candidate, summary=summary, narrative=narrative)


def profile_only_market_summary(
    unit: MarketUnit,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "brand": unit.brand_name,
        "source": unit.source,
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "profile_display": profile,
        "strength_items": [],
        "limitations": ["strategic strength candidate 0건: deterministic market_position 적용"],
        "candidate_count": len(candidates),
    }


def _target_metric_row(row: StrategicMetricRow, market_id: str) -> MetricRow:
    return MetricRow(
        brand_name=row.brand_name,
        brand_key=row.brand_key,
        source=row.source,
        measure=row.measure,
        atc4_code=market_id,
        raw_value_history=row.raw_value_history,
        channel_data=row.channel_data,
        specialty_data=row.specialty_data,
        channel_specialty_matrix=row.dimension_specialty_data,
        dimension_data=row.dimension_data,
    )


def _market_metric_row(row: StrategicMetricRow, market_id: str) -> MarketMetricRow:
    return MarketMetricRow(
        brand_key=row.brand_key,
        brand_name=row.brand_name,
        source=row.source,
        atc4_code=market_id,
        atc4_desc=market_id,
        raw_value_history=row.raw_value_history,
    )


def _native_candidate(unit: MarketUnit, candidate: dict[str, Any]) -> dict[str, Any]:
    period = str(candidate.get("period") or "unknown")
    return {
        **candidate,
        "source": unit.source,
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "slice": f"{'UBIST' if unit.source == 'ubist' else 'IQVIA'} "
        f"{'전략 ML' if unit.view_kind == 'market_landscape' else '전략 CD'} {unit.market_id}",
        "evidence": f"strategic_scope.{unit.view_kind}.{unit.market_id}.{unit.source}.{period}",
    }


def _ranked_keys(rows: list[StrategicMetricRow], latest_period: str) -> list[str]:
    return sorted(
        (row.brand_key for row in rows),
        key=lambda key: (
            -next(row for row in rows if row.brand_key == key).raw_value_history.get(latest_period, 0.0),
            -math.fsum(next(row for row in rows if row.brand_key == key).raw_value_history.values()),
            key,
        ),
    )


def _period_key(period: str) -> tuple[int, int, str]:
    month = re.fullmatch(r"(20\d{2})-(\d{2})", period)
    if month:
        return int(month.group(1)), int(month.group(2)), period
    quarter = re.fullmatch(r"(20\d{2})-Q([1-4])", period)
    if quarter:
        return int(quarter.group(1)), int(quarter.group(2)) * 3, period
    return 0, 0, period


def _consecutive_positive(history: dict[str, float], source: str) -> int:
    ordered = sorted(history, key=_period_key, reverse=True)
    count = 0
    for period in ordered:
        if history[period] <= 0:
            break
        count += 1
    return count
