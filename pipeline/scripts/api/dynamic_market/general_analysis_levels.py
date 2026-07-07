"""General-view source-specific analysis-level sections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sys
from typing import Any

from pymysql.err import MySQLError

from pipeline.scripts.api.dynamic_market.analysis_level_dimensions import build_analysis_rows
from pipeline.scripts.api.dynamic_market.analysis_level_series import metric_history_from_periods
from pipeline.scripts.api.dynamic_market.cause_time import SOURCE_LABELS
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder  # noqa: E402
from pipeline.scripts.etl.ubist_channel_resolver import resolve_market_channels  # noqa: E402


@dataclass(frozen=True, slots=True)
class GeneralLevelSpec:
    output_level: str
    canonical_level: str
    source_field: str


FIELD_BY_CANONICAL_LEVEL: dict[str, str] = {
    "Class": "class",
    "Molecule": "molecule",
    "제형/투여경로": "dosage_form",
    "용량": "strength_pack",
    "비/급여": "nhi_type",
    "Ox/Gx": "ox_gx",
}

GENERAL_LEVEL_SPECS: dict[str, tuple[GeneralLevelSpec, ...]] = {
    "ubist": (
        GeneralLevelSpec("판매사", "Class", "seller"),
        GeneralLevelSpec("성분용량", "Molecule", "molecule_strength"),
        GeneralLevelSpec("제형", "제형/투여경로", "form"),
        GeneralLevelSpec("투여경로", "용량", "route"),
        GeneralLevelSpec("급여구분", "비/급여", "reimbursement"),
    ),
    "iqvia_nsa": (
        GeneralLevelSpec("MFR NAME KOR", "Class", "mfr"),
        GeneralLevelSpec("MOLECULE TYPE", "Molecule", "molecule_type"),
        GeneralLevelSpec("MOLECULE DESC", "제형/투여경로", "molecule_desc"),
        GeneralLevelSpec("STRENGTH", "비/급여", "strength"),
        GeneralLevelSpec("NHI TYPE", "Ox/Gx", "nhi"),
    ),
}


def build_general_analysis_level_sections(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    mart_db: str,
) -> dict[str, Any] | None:
    specs = GENERAL_LEVEL_SPECS.get(metrics.source)
    if not specs:
        return None
    try:
        rows = build_analysis_rows(
            definition=definition,
            metrics=metrics,
            focus=focus,
            mart_db=mart_db,
        )
    except (MySQLError, RuntimeError, TypeError, ValueError, OSError):
        rows = _rows_from_metrics(metrics=metrics, focus=focus)
    if not rows:
        return None
    source_api = SOURCE_LABELS.get(metrics.source, metrics.source.upper())
    canonical_rows = [_with_canonical_dimension_aliases(row, specs) for row in rows]
    channels = list(cause_builder._channels_for_source(source_api))
    ubist_channel_context: dict[str, Any] | None = None
    if source_api == "UBIST":
        ubist_channel_context = resolve_market_channels(rows=canonical_rows, market={}, measure=metrics.measure)
    analysis_levels = _rename_analysis_levels(
        cause_builder._build_analysis_levels_from_mart(
            rows=canonical_rows,
            source=source_api,
            market=_synthetic_market(specs),
            view_source_id=None,
            target_name=None,
            fallback_level_top5={},
            channels_override=channels,
        ),
        specs,
    )
    canonical_levels = [spec.canonical_level for spec in specs]
    rows_by_level = cause_builder._level_rows_by_segment(canonical_rows, canonical_levels)
    level_top5_trend = _rename_level_top5_trend(
        cause_builder._level_top5_trend(
            _canonical_level_subset(analysis_levels, specs),
            canonical_rows,
            source_api,
            focus.brand_name if focus else None,
            rows_by_level=rows_by_level,
            include_all_options=bool(focus),
            channel="전체",
        ),
        specs,
    )
    status_channels = _market_status_channels(
        source=source_api,
        default_channels=channels,
        ubist_channel_context=ubist_channel_context,
    )
    market_status_levels = analysis_levels
    if status_channels != channels:
        market_status_levels = _rename_analysis_levels(
            cause_builder._build_analysis_levels_from_mart(
                rows=canonical_rows,
                source=source_api,
                market=_synthetic_market(specs),
                view_source_id=None,
                target_name=None,
                fallback_level_top5={},
                channels_override=status_channels,
            ),
            specs,
        )
    market_status = cause_builder._ensure_analysis_level_market_status_contract(
        cause_builder._analysis_level_market_status_by_channel(
            level_top5_trend=level_top5_trend,
            analysis_levels=market_status_levels,
            rows=canonical_rows,
            source=source_api,
            channels=status_channels,
            include_all_options=bool(focus),
        )
    )
    return {
        "analysis_levels": analysis_levels,
        "analysis_level_market_status": market_status,
        "level_top5_trend": level_top5_trend,
        "rows": canonical_rows,
        "ubist_channel_context": ubist_channel_context,
    }


def _with_canonical_dimension_aliases(row: dict[str, Any], specs: tuple[GeneralLevelSpec, ...]) -> dict[str, Any]:
    clone = dict(row)
    by_dimension = _json_object(clone.get("by_dimension"))
    dimension_data = _json_object(clone.get("dimension_data"))
    dimension_channel_data = _json_object(clone.get("dimension_channel_data"))
    dimension_specialty_data = _json_object(clone.get("dimension_specialty_data"))
    for spec in specs:
        canonical_field = FIELD_BY_CANONICAL_LEVEL[spec.canonical_level]
        source_value = by_dimension.get(spec.source_field)
        if source_value not in (None, "", [], {}):
            by_dimension[canonical_field] = source_value
        _copy_dimension_field(dimension_data, source=spec.source_field, target=canonical_field)
        _copy_dimension_field(dimension_channel_data, source=spec.source_field, target=canonical_field)
        _copy_dimension_field(dimension_specialty_data, source=spec.source_field, target=canonical_field)
    clone["by_dimension"] = _json_dump(by_dimension)
    clone["dimension_data"] = _json_dump(dimension_data)
    clone["dimension_channel_data"] = _json_dump(dimension_channel_data)
    if dimension_specialty_data:
        clone["dimension_specialty_data"] = _json_dump(dimension_specialty_data)
    for key in ("__by_dimension", "__dimension_data", "__dimension_channel_data", "__dimension_specialty_data"):
        clone.pop(key, None)
    return clone


def _rows_from_metrics(*, metrics: AggregatedMetrics, focus: BrandMetric | None) -> list[dict[str, Any]]:
    totals_by_period = {
        str(item["period"]): float(item.get("market_size") or 0.0)
        for item in metrics.monthly_series
    }
    rows: list[dict[str, Any]] = []
    for brand in metrics.all_brands:
        row = dict(brand.analysis_row)
        row["brand_key"] = brand.brand_key
        row["brand_name"] = brand.brand_name
        row["atc4_code"] = brand.atc4_code
        row["source"] = metrics.source
        row["measure"] = metrics.measure
        row["unit_label"] = metrics.unit_label
        row["is_target"] = bool(focus and brand.brand_key == focus.brand_key)
        row["is_jw"] = bool(focus and brand.brand_key == focus.brand_key)
        row["metric_history"] = metric_history_from_periods(
            history_by_period=brand.history_by_period,
            totals_by_period=totals_by_period,
            rank=brand.rank,
        )
        rows.append(row)
    return rows


def _copy_dimension_field(payload: dict[str, Any], *, source: str, target: str) -> None:
    value = payload.get(source)
    if isinstance(value, dict):
        payload[target] = value


def _rename_analysis_levels(payload: dict[str, Any], specs: tuple[GeneralLevelSpec, ...]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    renamed_data = {
        spec.output_level: data.get(spec.canonical_level, {"segments": [], "by_channel": {}})
        for spec in specs
    }
    return {
        **payload,
        "levels": [spec.output_level for spec in specs],
        "data": renamed_data,
    }


def _canonical_level_subset(payload: dict[str, Any], specs: tuple[GeneralLevelSpec, ...]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    canonical_data = {
        spec.canonical_level: data.get(spec.output_level, {"segments": [], "by_channel": {}})
        for spec in specs
    }
    return {
        **payload,
        "levels": [spec.canonical_level for spec in specs],
        "data": canonical_data,
    }


def _rename_level_top5_trend(payload: dict[str, Any], specs: tuple[GeneralLevelSpec, ...]) -> dict[str, Any]:
    by_level = payload.get("by_level") if isinstance(payload.get("by_level"), dict) else {}
    renamed_by_level: dict[str, Any] = {}
    for spec in specs:
        value = dict(by_level.get(spec.canonical_level) or {})
        value["level_label"] = spec.output_level
        renamed_by_level[spec.output_level] = value
    return {
        **payload,
        "available_levels": [{"key": spec.output_level, "label": spec.output_level} for spec in specs],
        "default_level": specs[0].output_level if specs else None,
        "by_level": renamed_by_level,
    }


def _synthetic_market(specs: tuple[GeneralLevelSpec, ...]) -> dict[str, int]:
    canonical_levels = {spec.canonical_level for spec in specs}
    return {
        "analyze_class": int("Class" in canonical_levels),
        "analyze_molecule": int("Molecule" in canonical_levels),
        "analyze_dosage_form": int("제형/투여경로" in canonical_levels),
        "analyze_strength_pack": int("용량" in canonical_levels),
        "analyze_nhi_type": int("비/급여" in canonical_levels),
        "analyze_ox_gx": int("Ox/Gx" in canonical_levels),
    }


def _market_status_channels(
    *,
    source: str,
    default_channels: list[str],
    ubist_channel_context: dict[str, Any] | None,
) -> list[str]:
    if source == "UBIST" and isinstance(ubist_channel_context, dict):
        specialty_channels = ubist_channel_context.get("specialty_channels")
        if isinstance(specialty_channels, list) and specialty_channels:
            return [str(channel) for channel in specialty_channels if str(channel)]
    return default_channels


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
