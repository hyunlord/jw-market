"""Build ``/api/cause``-compatible payloads for runtime dynamic markets."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import logging
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any

from pipeline.scripts.api.config import config
from pipeline.scripts.api.competitor_ranking import (
    MAX_COMPETITOR_COUNT,
    CompetitorRankItem,
    select_top_competitors,
)
from pipeline.scripts.api.dynamic_market.analysis_levels import build_analysis_level_sections
from pipeline.scripts.api.dynamic_market.cause_ranking import brand_ranking, company_hhi_series, company_ranking
from pipeline.scripts.api.dynamic_market.cause_sections import (
    display_matrix_rows,
    growth_contribution,
    kpi,
    matrix_rows,
)
from pipeline.scripts.api.dynamic_market.cause_time import (
    MEASURE_LABEL,
    SOURCE_LABELS,
    avg_share,
    empty_analysis_levels,
    hhi_series,
    history,
    latest_hhi,
    latest_market_value,
    market_size_series,
    recent_yoy,
)
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition, PeriodRange
from pipeline.scripts.api.market_growth import growth_endpoint_meta
from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code, raw_pair_to_channel_code


logger = logging.getLogger(__name__)


def _stage_timing_enabled() -> bool:
    return os.getenv("LATENCY_STAGE_TIMING", "").strip().lower() in {"1", "true", "yes"}


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder  # noqa: E402


PORTAL_UNUSED_DATA_KEYS = frozenset({"data_period_coverage"})
GENERAL_UNUSED_DATA_KEYS = frozenset(
    {
        "market_yoy_recent_pct",
        "market_yoy_series",
        "target_customer_competition_by_channel",
        "ubist_specialty_channels",
        "ubist_specialty_target_channels",
    }
)


def build_cause_payload(
    *, definition: MarketDefinition, metrics: AggregatedMetrics, period_range: PeriodRange = PeriodRange()
) -> dict[str, Any]:
    """Return a runtime payload with the same field tree as ``/api/cause``."""

    source = SOURCE_LABELS.get(metrics.source, metrics.source.upper())
    market_id = _market_id(definition)
    focus = _focus_brand(metrics.all_brands, definition.focus_brand_key)
    data = build_cause_data(definition=definition, metrics=metrics, focus=focus, period_range=period_range)
    meta = build_market_meta(definition=definition, metrics=metrics, market_id=market_id, data=data)
    payload = {
        "brand": focus.brand_name if focus else "동적 시장",
        "brand_key": focus.brand_key if focus else market_id,
        "brand_name": focus.brand_name if focus else "동적 시장",
        "data": data,
        "market_id": market_id,
        "market_meta": meta,
        "markets": [{"market_id": market_id, "is_primary": True}],
        "measure": metrics.measure,
        "source": source,
        "unit_label": metrics.unit_label,
        "view": definition.view,
    }
    return normalize_portal_read_payload(payload)


def build_cause_data(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    period_range: PeriodRange = PeriodRange(),
) -> dict[str, Any]:
    """Build all direct ``data`` keys expected by the cause renderer."""

    timing_enabled = _stage_timing_enabled()
    started = perf_counter() if timing_enabled else None
    series = market_size_series(metrics)
    yoy_series = {item["period"]: item["yoy_growth_pct"] for item in series}
    hhi = hhi_series(metrics.all_brands, source=metrics.source)
    full_matrix = matrix_rows(metrics=metrics, focus=focus)
    matrix = full_matrix[:100]
    display_matrix = display_matrix_rows(matrix, focus=focus)
    brand_cohort = tuple(
        str(row["brand"])
        for row in display_matrix
        if row.get("brand") not in (None, "")
    )
    ranking = brand_ranking(metrics.all_brands, focus=focus)
    company = company_ranking(metrics.all_brands)
    company_hhi = company_hhi_series(metrics.all_brands, source=metrics.source)
    if timing_enabled and started is not None:
        logger.info(
            "market_latency_compose_stage section=base_metrics ms=%.3f brands=%d",
            (perf_counter() - started) * 1000,
            len(metrics.all_brands),
        )
    analysis_started = perf_counter() if timing_enabled else None
    levels = empty_analysis_levels(series)
    analysis_sections = build_analysis_level_sections(
        definition=definition,
        metrics=metrics,
        focus=focus,
        mart_db=config.db_name,
        period_range=period_range,
        brand_cohort=brand_cohort,
    )
    if timing_enabled and analysis_started is not None:
        logger.info(
            "market_latency_compose_stage section=analysis_levels ms=%.3f present=%s",
            (perf_counter() - analysis_started) * 1000,
            bool(analysis_sections),
        )
    if analysis_sections:
        levels = analysis_sections["analysis_levels"]
    ubist_channels = _general_ubist_channels(metrics)
    if analysis_sections and isinstance(analysis_sections.get("ubist_channel_context"), dict):
        ubist_channels = _ubist_channels_from_context(analysis_sections["ubist_channel_context"], fallback=ubist_channels)
    target_channels = _general_target_customer_channels(metrics=metrics, ubist_channels=ubist_channels)
    competition_started = perf_counter() if timing_enabled else None
    target_competition_by_channel = _target_customer_competition_by_channel(
        analysis_sections=analysis_sections,
        metrics=metrics,
        focus=focus,
        channels=target_channels,
        brand_cohort=brand_cohort,
    )
    if timing_enabled and competition_started is not None:
        logger.info(
            "market_latency_compose_stage section=target_competition ms=%.3f channels=%d",
            (perf_counter() - competition_started) * 1000,
            len(target_channels),
        )
    if definition.view == "general":
        hhi_recent = latest_hhi(metrics.all_brands)
    else:
        hhi_recent = hhi[-1]["hhi"] if hhi else latest_hhi(metrics.all_brands)
    population = _population_layers(
        metrics=metrics,
        focus=focus,
        latest_period=str(series[-1]["period"]) if series else None,
    )
    matrix_payload = {
        "data": display_matrix,
        "ms_avg_pct": avg_share(display_matrix),
        "share_avg_pct": avg_share(display_matrix),
    }
    data = {
        "analysis_level_market_status": (
            analysis_sections["analysis_level_market_status"] if analysis_sections else levels
        ),
        "analysis_levels": levels,
        "brand_ranking": ranking,
        "brand_ranking_stacked": ranking,
        "company_concentration_trend": {
            "periods": [item["year"] for item in company_hhi],
            "hhi_values": [item["hhi"] for item in company_hhi],
        },
        "company_ranking": company,
        "company_ranking_stacked": company,
        "ei_ms_matrix": matrix_payload,
        "growth_contribution": growth_contribution(
            metrics.all_brands,
            focus=focus,
            source=metrics.source,
        ),
        "growth_contribution_ms_matrix": matrix_payload,
        "hhi_recent": hhi_recent,
        "hhi_series_5y": hhi,
        **population,
        "kpi": kpi(metrics=metrics, matrix=full_matrix, focus=focus, hhi_recent=hhi_recent),
        "level_top5_trend": (
            analysis_sections["level_top5_trend"]
            if analysis_sections
            else {
                "available_levels": [],
                "default_level": None,
                "by_level": {},
                "note": "동적 일반뷰는 ATC4/molecule filter로 정의되므로 MI Master 전략 레벨 overlay를 적용하지 않는다.",
            }
        ),
        "market_size_series": series,
        "market_yoy_recent_pct": recent_yoy(series),
        "market_yoy_series": yoy_series,
        "sources_data": {
            "periods_unit": "월",
            "periods_count": len(series),
            "market_size_series": series,
            "market_yoy_series": yoy_series,
            "market_yoy_recent_pct": recent_yoy(series),
            "hhi_series_5y": hhi,
            "hhi_recent": hhi_recent,
            "cagr_5y_pct": metrics.cagr,
        },
        "target_customer_competition": target_competition_by_channel or {
            "available_in_view": [],
            "target_type": "동적 시장",
            "targets": [],
            "views": [],
            "note": "동적 일반뷰 MVP에는 전략뷰 target customer overlay가 없다.",
        },
        "target_customer_competition_by_channel": target_competition_by_channel,
        "ubist_specialty_channels": ubist_channels["specialty_channels"],
        "ubist_specialty_target_channels": ubist_channels["specialty_target_channels"],
    }
    if definition.channel_axis and definition.channel_axis.is_active and definition.channel_axis.source == "iqvia_nsa":
        data["iqvia_audit_code_channels"] = _general_iqvia_audit_codes(metrics)
    if definition.focus_brand_key and not data["kpi"]:
        data["kpi_reason"] = "focus_not_found"
    normalized = normalize_portal_read_data(data)
    return slim_general_response_data(normalized) if definition.view == "general" else normalized


def _population_layers(
    *,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    latest_period: str | None,
) -> dict[str, Any]:
    members = list(metrics.all_brands)
    active = [
        brand
        for brand in members
        if latest_period is not None and history(brand).get(latest_period, 0.0) > 0
    ]
    displayed = list(
        select_top_competitors(
            tuple(
                CompetitorRankItem(brand.brand_key, brand.total_value, brand)
                for brand in members
            ),
            selected_brand_key=focus.brand_key if focus else None,
        )
    )
    has_others = len(displayed) < len(members)

    def identity(brand: BrandMetric) -> dict[str, str]:
        return {"brand_key": brand.brand_key, "brand": brand.brand_name}

    display_members = [
        {**identity(brand), "is_others": False}
        for brand in displayed
    ]
    if has_others:
        display_members.append({"brand_key": None, "brand": "기타", "is_others": True})
    return {
        "member_population": {
            "count": len(members),
            "members": [identity(brand) for brand in members],
        },
        "active_members": {
            "period": latest_period,
            "count": len(active),
            "members": [identity(brand) for brand in active],
        },
        "display_members": {
            "top_n": MAX_COMPETITOR_COUNT,
            "count": len(display_members),
            "has_others": has_others,
            "members": display_members,
        },
    }


def slim_general_response_data(data: dict[str, Any]) -> dict[str, Any]:
    """Remove fields proven unused by portal and repository runtime consumers."""

    slimmed = {key: value for key, value in data.items() if key not in GENERAL_UNUSED_DATA_KEYS}
    trend = slimmed.get("level_top5_trend")
    if not isinstance(trend, dict):
        return slimmed
    by_level = trend.get("by_level")
    if not isinstance(by_level, dict):
        return slimmed

    slimmed_by_level: dict[str, Any] = {}
    for level_name, raw_level in by_level.items():
        if not isinstance(raw_level, dict):
            slimmed_by_level[level_name] = raw_level
            continue
        level = {
            key: value
            for key, value in raw_level.items()
            if key not in {"level_label", "level_value", "total_market_value"}
        }
        values = level.get("values")
        if isinstance(values, list):
            level["values"] = [_slim_level_top5_value(value) for value in values]
        slimmed_by_level[level_name] = level
    slimmed["level_top5_trend"] = {**trend, "by_level": slimmed_by_level}
    return slimmed


def _slim_level_top5_value(raw_value: Any) -> Any:
    if not isinstance(raw_value, dict):
        return raw_value
    value = {key: item for key, item in raw_value.items() if key != "total_volume"}
    brands = value.get("brands_in_value")
    if isinstance(brands, list):
        value["brands_in_value"] = [
            (
                {
                    key: item
                    for key, item in brand.items()
                    if key not in {"volume_recent", "volume_series_10pt"}
                }
                if isinstance(brand, dict)
                else brand
            )
            for brand in brands
        ]
    return value


def _ubist_channels_from_context(context: dict[str, Any], *, fallback: dict[str, list[Any]]) -> dict[str, list[Any]]:
    specialty_channels = context.get("specialty_channels")
    specialty_target_channels = context.get("specialty_target_channels")
    if not isinstance(specialty_channels, list) or not specialty_channels:
        return fallback
    return {
        "specialty_channels": specialty_channels,
        "specialty_target_channels": specialty_target_channels if isinstance(specialty_target_channels, list) else [],
    }


def _target_customer_competition_by_channel(
    *,
    analysis_sections: dict[str, Any] | None,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    channels: list[Any],
    brand_cohort: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if metrics.source not in {"ubist", "iqvia_nsa"} or not analysis_sections or not channels:
        return {}
    rows = analysis_sections.get("rows")
    if not isinstance(rows, list):
        return {}
    periods = [str(item["period"]) for item in market_size_series(metrics)]
    return cause_builder._target_customer_competition(
        rows=rows,
        source=SOURCE_LABELS.get(metrics.source, metrics.source.upper()),
        target_name=focus.brand_name if focus else None,
        periods=periods,
        channels=[str(channel) for channel in channels if str(channel)],
        series_value_cache=analysis_sections.get("series_value_cache"),
        channel_rows_cache=analysis_sections.get("channel_rows_cache"),
        brand_cohort=brand_cohort,
    )


def _general_target_customer_channels(
    *,
    metrics: AggregatedMetrics,
    ubist_channels: dict[str, list[Any]],
) -> list[Any]:
    if metrics.source == "ubist":
        return ubist_channels["specialty_channels"]
    if metrics.source != "iqvia_nsa":
        return []
    channels = _general_iqvia_audit_channel_names(metrics)
    return channels or cause_builder._channels_for_source("IQVIA")


def _general_ubist_channels(metrics: AggregatedMetrics, *, max_channels: int = 4) -> dict[str, list[Any]]:
    """Return general-view UBIST top channels from raw matrix when present.

    General views have no MI Master target slots, so they intentionally use the
    existing general GH parser where general hospitals include hospital rows.
    Ranking uses the latest raw period, matching the runtime-fill rule; only
    sidecar-filter rows without raw matrix fall back to the legacy mart totals.
    """

    if metrics.source != "ubist":
        return {"specialty_channels": [], "specialty_target_channels": []}
    if metrics.ubist_specialty_channels:
        return {
            "specialty_channels": list(metrics.ubist_specialty_channels),
            "specialty_target_channels": list(metrics.ubist_specialty_target_channels),
        }

    channels = _general_ubist_channels_from_raw_matrix(metrics, max_channels=max_channels)
    if channels:
        return channels

    totals_by_code: dict[str, float] = {}
    for brand in metrics.all_brands:
        for code, series in brand.ubist_channel_by_code.items():
            parsed = parse_channel_code(code)
            if parsed is None:
                continue
            totals_by_code[parsed.code] = totals_by_code.get(parsed.code, 0.0) + sum(
                float(value or 0.0) for value in series.values()
            )

    channels = []
    used: set[str] = set()
    for code, _value in sorted(totals_by_code.items(), key=lambda item: item[1], reverse=True):
        if len(channels) >= max_channels:
            break
        parsed = parse_channel_code(code)
        if parsed is None or parsed.code in used:
            continue
        channels.append(parsed)
        used.add(parsed.code)

    if not channels:
        return {"specialty_channels": [], "specialty_target_channels": []}
    return {
        "specialty_channels": ["전체", *[channel.display_name for channel in channels]],
        "specialty_target_channels": [channel.as_dict() for channel in channels],
    }


def _general_ubist_channels_from_raw_matrix(metrics: AggregatedMetrics, *, max_channels: int) -> dict[str, list[Any]]:
    latest_period = _latest_matrix_period(metrics)
    if latest_period is None:
        return {}
    totals_by_code: dict[str, float] = {}
    for brand in metrics.all_brands:
        for facility, specialties in brand.channel_specialty_matrix.items():
            for specialty, series in specialties.items():
                code = raw_pair_to_channel_code(facility, specialty)
                if not code:
                    continue
                parsed = parse_channel_code(code)
                if parsed is None:
                    continue
                totals_by_code[parsed.code] = totals_by_code.get(parsed.code, 0.0) + float(series.get(latest_period) or 0.0)
    channels = []
    used: set[str] = set()
    for code, _value in sorted(totals_by_code.items(), key=lambda item: item[1], reverse=True):
        if len(channels) >= max_channels:
            break
        parsed = parse_channel_code(code)
        if parsed is None or parsed.code in used:
            continue
        channels.append(parsed)
        used.add(parsed.code)
    if not channels:
        return {}
    return {
        "specialty_channels": ["전체", *[channel.display_name for channel in channels]],
        "specialty_target_channels": [channel.as_dict() for channel in channels],
    }


def _latest_matrix_period(metrics: AggregatedMetrics) -> str | None:
    periods: set[str] = set()
    for brand in metrics.all_brands:
        for specialties in brand.channel_specialty_matrix.values():
            for series in specialties.values():
                periods.update(str(period) for period in series)
    return max(periods) if periods else None


def _general_iqvia_audit_codes(metrics: AggregatedMetrics) -> list[dict[str, Any]]:
    """Return selected IQVIA audit-code summaries from the raw audit matrix."""

    latest_period = _latest_audit_matrix_period(metrics)
    summaries: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    latest_values: dict[str, float] = {}
    for brand in metrics.all_brands:
        for audit_code, series in brand.audit_code_matrix.items():
            totals[audit_code] = totals.get(audit_code, 0.0) + sum(float(value or 0.0) for value in series.values())
            if latest_period is not None:
                latest_values[audit_code] = latest_values.get(audit_code, 0.0) + float(series.get(latest_period) or 0.0)
    for audit_code, total_value in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        summaries.append(
            {
                "audit_code": audit_code,
                "latest_period": latest_period,
                "latest_value": latest_values.get(audit_code, 0.0) if latest_period is not None else None,
                "total_value": total_value,
            }
        )
    return summaries


def _general_iqvia_audit_channel_names(metrics: AggregatedMetrics) -> list[str]:
    channels = [item["audit_code"] for item in _general_iqvia_audit_codes(metrics) if item.get("audit_code")]
    if not channels:
        return []
    ordered = [channel for channel in cause_builder._channels_for_source("IQVIA") if channel == "전체" or channel in channels]
    if "전체" not in ordered:
        ordered.insert(0, "전체")
    return ordered


def _latest_audit_matrix_period(metrics: AggregatedMetrics) -> str | None:
    periods: set[str] = set()
    for brand in metrics.all_brands:
        for series in brand.audit_code_matrix.values():
            periods.update(str(period) for period in series)
    return max(periods) if periods else None


def normalize_portal_read_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the audited portal-read cause contract without changing metric values."""

    data = payload.get("data")
    if isinstance(data, dict):
        payload["data"] = normalize_portal_read_data(data)
    payload.pop("resolved_scope", None)
    return payload


def normalize_portal_read_data(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only portal-read data shape and add the split-class ``Class`` alias."""

    normalized = {key: value for key, value in data.items() if key not in PORTAL_UNUSED_DATA_KEYS}
    for key in ("analysis_levels", "analysis_level_market_status"):
        section = normalized.get(key)
        if isinstance(section, dict):
            normalized[key] = _ensure_class_alias(section)
    return normalized


def _ensure_class_alias(section: dict[str, Any]) -> dict[str, Any]:
    """Expose the detailed split class while preserving portal chart compatibility."""

    data = section.get("data")
    if not isinstance(data, dict) or "Class" in data:
        return section
    if "Class 2" in data:
        return {**section, "data": {**data, "Class": deepcopy(data["Class 2"])}}
    if "Class 1" in data:
        return {**section, "data": {**data, "Class": deepcopy(data["Class 1"])}}
    return section


def build_market_meta(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    market_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Build the same market metadata keys returned by ``/api/cause``."""

    atc_codes = list(definition.filter_echo.get("atc4", []))
    molecules = list(definition.normalized_molecules)
    source = SOURCE_LABELS.get(metrics.source, metrics.source.upper())
    label = _market_label(atc_codes=atc_codes, molecules=molecules)
    if definition.view.startswith("strategic_"):
        label = f"전략 동적 시장: {market_id}"
    growth_values = {
        str(item["period"]): item.get("market_size")
        for item in metrics.monthly_series
    }
    return {
        "strategic_market_id": market_id,
        "market_name": label,
        "market_name_short": "동적 시장",
        "market_label_kor": label,
        "market_definition_label": label,
        "market_definition_full": _market_definition_full(definition=definition, atc_codes=atc_codes, molecules=molecules),
        "filters": definition.filter_echo,
        "view": definition.view,
        "mkt_team": "Runtime",
        "brand_list": [item.brand_name for item in metrics.all_brands[:100]],
        "atc_codes": atc_codes,
        "view_source_id": _view_source_id(definition),
        "atc_count": len(atc_codes) or None,
        "nhi_type": None,
        "sources": [source],
        "source_label": source,
        "is_dual_source": False,
        "measures": sorted(_valid_measures(metrics.source)),
        "measures_label": {"primary": MEASURE_LABEL.get(metrics.measure, metrics.measure), "secondary": None},
        "available_levels": [],
        "direct_competition_count": len(metrics.all_brands),
        "market_size_recent": latest_market_value(data["market_size_series"]),
        "market_cagr_5y_pct": data.get("kpi", {}).get("market_cagr_5y_pct"),
        "mom_growth_meta": growth_endpoint_meta(growth_values),
        "is_jw": False,
        "is_target": False,
    }


def _market_id(definition: MarketDefinition) -> str:
    if definition.strategic_market_id:
        return definition.strategic_market_id
    fingerprint = repr(sorted((key, value) for key, value in definition.filter_echo.items()))
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
    return f"dynamic_general_{digest}"


def _view_source_id(definition: MarketDefinition) -> str:
    return definition.view if definition.view.startswith("strategic_") else "dynamic_general"


def _market_definition_full(*, definition: MarketDefinition, atc_codes: list[str], molecules: list[str]) -> str:
    if definition.view.startswith("strategic_"):
        return f"strategic_view={definition.view}; market_id={definition.strategic_market_id}; narrowing=analysis_level"
    return f"ATC4={', '.join(atc_codes) or '-'}; molecule={', '.join(molecules) or '-'}"


def _market_label(*, atc_codes: list[str], molecules: list[str]) -> str:
    parts = []
    if atc_codes:
        parts.append("ATC4 " + ", ".join(atc_codes))
    if molecules:
        parts.append("성분 " + ", ".join(molecules))
    return "동적 시장: " + " · ".join(parts) if parts else "동적 시장"


def _valid_measures(source: str) -> set[str]:
    return {"sales", "volume"} if source == "ubist" else {"sales", "unit", "dosage_unit", "counting_unit"}


def _focus_brand(brands: tuple[BrandMetric, ...], focus_brand_key: str | None) -> BrandMetric | None:
    if focus_brand_key:
        requested = focus_brand_key.strip()
        for brand in brands:
            if brand.brand_key == requested:
                return brand
        return None
    return brands[0] if brands else None
