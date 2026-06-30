"""Build ``/api/cause``-compatible payloads for runtime dynamic markets."""

from __future__ import annotations

import hashlib
from typing import Any

from pipeline.scripts.api.dynamic_market.cause_sections import (
    brand_ranking,
    company_ranking,
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
    latest_hhi,
    latest_market_value,
    market_size_series,
    recent_yoy,
)
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition


PORTAL_UNUSED_DATA_KEYS = frozenset({"data_period_coverage"})


def build_cause_payload(*, definition: MarketDefinition, metrics: AggregatedMetrics) -> dict[str, Any]:
    """Return a runtime payload with the same field tree as ``/api/cause``."""

    source = SOURCE_LABELS.get(metrics.source, metrics.source.upper())
    market_id = _market_id(definition)
    focus = _focus_brand(metrics.all_brands, definition.focus_brand_key)
    data = build_cause_data(definition=definition, metrics=metrics, focus=focus)
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
        "view": str(definition.filter_echo.get("view_kind") or "market_landscape"),
    }
    return normalize_portal_read_payload(payload)


def build_cause_data(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
) -> dict[str, Any]:
    """Build all direct ``data`` keys expected by the cause renderer."""

    del definition
    series = market_size_series(metrics)
    yoy_series = {item["period"]: item["yoy_growth_pct"] for item in series}
    hhi = hhi_series(metrics.all_brands, source=metrics.source)
    matrix = matrix_rows(metrics=metrics, focus=focus)
    ranking = brand_ranking(metrics.all_brands, focus=focus)
    company = company_ranking(metrics.all_brands)
    levels = empty_analysis_levels(series)
    hhi_recent = hhi[-1]["hhi"] if hhi else latest_hhi(metrics.all_brands)
    data = {
        "analysis_level_market_status": levels,
        "analysis_levels": levels,
        "brand_ranking": ranking,
        "brand_ranking_stacked": ranking,
        "company_concentration_trend": {
            "periods": [item["year"] for item in hhi],
            "hhi_values": [item["hhi"] for item in hhi],
        },
        "company_ranking": company,
        "company_ranking_stacked": company,
        "ei_ms_matrix": {"data": matrix, "ms_avg_pct": avg_share(matrix), "share_avg_pct": avg_share(matrix)},
        "growth_contribution": growth_contribution(metrics.all_brands, focus=focus),
        "growth_contribution_ms_matrix": {"data": matrix, "ms_avg_pct": avg_share(matrix), "share_avg_pct": avg_share(matrix)},
        "hhi_recent": hhi_recent,
        "hhi_series_5y": hhi,
        "kpi": kpi(metrics=metrics, matrix=matrix, focus=focus, hhi_recent=hhi_recent),
        "level_top5_trend": {
            "available_levels": [],
            "default_level": None,
            "by_level": {},
            "note": "동적 일반뷰는 ATC4/molecule filter로 정의되므로 MI Master 전략 레벨 overlay를 적용하지 않는다.",
        },
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
        "target_customer_competition": {
            "available_in_view": [],
            "target_type": "동적 시장",
            "targets": [],
            "views": [],
            "note": "동적 일반뷰 MVP에는 전략뷰 target customer overlay가 없다.",
        },
        "target_customer_competition_by_channel": {},
        "ubist_specialty_channels": [],
        "ubist_specialty_target_channels": [],
    }
    return normalize_portal_read_data(data)


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
    """Mirror the cache builder's split-class alias for portal chart compatibility."""

    data = section.get("data")
    if not isinstance(data, dict) or "Class" in data or "Class 1" not in data:
        return section
    return {**section, "data": {**data, "Class": data["Class 1"]}}


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
    return {
        "strategic_market_id": market_id,
        "market_name": label,
        "market_name_short": "동적 시장",
        "market_label_kor": label,
        "market_definition_label": label,
        "market_definition_full": _market_definition_full(definition=definition, atc_codes=atc_codes, molecules=molecules),
        "filters": definition.filter_echo,
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
        "market_cagr_5y_pct": metrics.cagr,
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
    return brands[0] if brands else None
