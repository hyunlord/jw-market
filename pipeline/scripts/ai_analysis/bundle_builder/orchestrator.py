from __future__ import annotations

from datetime import datetime
import json

from .brand_context_builder import build_brand_context, find_market_ids_for_brand
from .competitor_events_builder import build_competitor_events
from .competitor_context_builder import build_competitor_context
from .config import BundleConfig
from .event_bundle_builder import build_event_bundle
from .hash_util import compute_bundle_hash, deterministic_json_dumps
from .market_context_builder import build_market_context
from .market_views_orchestrator import build_market_views


FORECAST_HORIZON_INDICES = {
    "UBIST": {"1y": 11, "3y": 35, "5y": 59},
    "IQVIA": {"1y": 3, "3y": 11, "5y": 19},
}


def _compute_stats(bundle: dict) -> dict:
    direct_count = len(bundle["event_bundle"]["direct_events"])
    cross_count = len(bundle["event_bundle"]["cross_match_events"])
    competitor_count = len(bundle["competitor_context"]["competitors"])
    month_keys = set()
    for metric in bundle["market_context"]["brand_metrics"].values():
        month_keys.update((metric.get("history") or {}).keys())
    rough = {k: v for k, v in bundle.items() if k != "bundle_meta"}
    estimated_tokens = int(len(deterministic_json_dumps(rough)) / 3.5)
    return {
        "event_count_direct": direct_count,
        "event_count_cross": cross_count,
        "competitor_count": competitor_count,
        "market_metric_months": len(month_keys),
        "estimated_tokens": estimated_tokens,
    }


def _compute_stats_v1_1(bundle: dict) -> dict:
    event_bundle = bundle["event_bundle"]
    competitor_sources = bundle["competitor_events"]["by_source"]
    competitor_total = sum(
        len(comp.get("events") or [])
        for source in competitor_sources.values()
        for comp in source.get("competitors", [])
    )
    rough = {k: v for k, v in bundle.items() if k != "bundle_meta"}
    return {
        "competitor_count_ubist": len((competitor_sources.get("UBIST") or {}).get("competitors", [])),
        "competitor_count_iqvia": len((competitor_sources.get("IQVIA") or {}).get("competitors", [])),
        "event_count_brand_centric": len(event_bundle["events_brand_centric"]),
        "event_count_market_trend": len(event_bundle["events_market_trend"]),
        "event_count_cross": len(event_bundle["cross_match_events"]),
        "event_count_competitor": competitor_total,
        "estimated_tokens": int(len(deterministic_json_dumps(rough)) / 3.5),
    }


def _mart_computed_at(db_conn) -> str | None:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(computed_at) AS computed_at
            FROM (
              SELECT MAX(computed_at) AS computed_at FROM mart_strategic_ml_brand_metric
              UNION ALL
              SELECT MAX(computed_at) AS computed_at FROM mart_strategic_ml_market_metric
              UNION ALL
              SELECT MAX(computed_at) AS computed_at FROM mart_strategic_cd_brand_metric
              UNION ALL
              SELECT MAX(computed_at) AS computed_at FROM mart_strategic_cd_market_metric
            ) t
            """
        )
        row = cur.fetchone()
    value = row.get("computed_at") if row else None
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _forecast_placeholder() -> dict:
    return {
        "available": False,
        "by_view": {},
        "schema_version": "v1_1_placeholder",
        "note": "Phase 23+ 또는 Phase η 의 시계열 예측 결과가 적재되면 활성화. cache_deep_analysis.response_json.data.{forecast, simulation} 에서 view_id 별로 추출 예정.",
        "expected_structure_when_available": {
            "available": True,
            "by_view": {
                "ML.UBIST.sales": {
                    "forecast": {
                        "history_periods": [],
                        "forecast_periods": [],
                        "brands": [],
                    },
                    "simulation": {
                        "scenarios": {
                            "base": {},
                            "upper": {},
                            "lower": {},
                        },
                        "anomaly_signals": [],
                    },
                }
            },
        },
    }


def _json_load(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)


def _load_deep_analysis_payload(brand: str, db_conn) -> dict:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT response_json
            FROM cache_deep_analysis
            WHERE brand=%s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (brand,),
        )
        row = cur.fetchone()
    return _json_load(row.get("response_json")) if row else {}


def _scenario_value(entry: dict, scenario: str, index: int) -> float | int | None:
    values = ((entry.get("scenarios") or {}).get(scenario) or {}).get("values") or []
    if index >= len(values):
        return None
    value = values[index]
    return value if isinstance(value, (int, float)) else None


def _summarize_simulation_entry(entry: dict, combo_payload: dict, source: str) -> dict | None:
    periods = entry.get("forecast_periods") or []
    horizon_indices = FORECAST_HORIZON_INDICES.get(source.upper())
    if not horizon_indices:
        return None

    summary = {
        "combo": combo_payload.get("combo"),
        "source": source.upper(),
        "measure": (combo_payload.get("combo") or ".").split(".", 1)[-1],
        "unit": combo_payload.get("unit_label") or "KRW",
        "period_unit": combo_payload.get("period_unit"),
        "source_granularity": combo_payload.get("source_granularity"),
        "target_brand": combo_payload.get("target_brand"),
        "model": entry.get("model") or {},
        "confidence": entry.get("confidence") or {},
        "horizon_ci_levels": entry.get("horizon_ci_levels") or {},
        "ci_definition": (
            "base=selected model point forecast; ci_lower_95/ci_upper_95=95% 신뢰구간 하한/상한. "
            "사업 시나리오가 아닌 통계 예측 범위입니다."
        ),
        "raw_value_policy": "raw_krw_no_unit_conversion",
        "momentum": entry.get("momentum") or {},
        "warnings": entry.get("warnings") or [],
    }

    for label, index in horizon_indices.items():
        base = _scenario_value(entry, "base", index)
        lower = _scenario_value(entry, "lower", index)
        upper = _scenario_value(entry, "upper", index)
        if base is None or lower is None or upper is None or index >= len(periods):
            return None
        summary[f"horizon_{label}"] = {
            "period": periods[index],
            "base": base,
            "ci_lower_95": lower,
            "ci_upper_95": upper,
            "unit": combo_payload.get("unit_label") or "KRW",
        }
    return summary


def _build_forecast_simulation(brand: str, market_views: list[dict], config: BundleConfig, db_conn) -> dict:
    if not config.forecast_simulation.enabled:
        return _forecast_placeholder()
    if config.forecast_simulation.source != "cache_deep_analysis":
        placeholder = _forecast_placeholder()
        placeholder["note"] = f"Unsupported forecast_simulation source: {config.forecast_simulation.source}"
        return placeholder

    payload = _load_deep_analysis_payload(brand, db_conn)
    by_combo = (((payload.get("data") or {}).get("simulation") or {}).get("by_combo") or {})
    by_view: dict[str, dict] = {}
    for view in market_views:
        view_id = str(view.get("view_id") or "")
        if not view_id.startswith("ML."):
            continue
        source = str(view.get("source") or "").upper()
        measure = str(view.get("measure") or "")
        combo_key = f"{source}.{measure}"
        combo_payload = by_combo.get(combo_key) or {}
        by_brand = combo_payload.get("by_brand") or {}
        entry = by_brand.get(brand) or by_brand.get(combo_payload.get("target_brand"))
        if not entry:
            continue
        summary = _summarize_simulation_entry(entry, combo_payload, source)
        if summary:
            by_view[view_id] = summary

    if not by_view:
        placeholder = _forecast_placeholder()
        placeholder["note"] = "cache_deep_analysis.response_json.data.simulation.by_combo 에 매핑 가능한 ML view 예측 요약이 없습니다."
        return placeholder
    return {
        "available": True,
        "by_view": by_view,
        "schema_version": "v1_2_simulation_summary",
        "source": "cache_deep_analysis.response_json.data.simulation.by_combo",
        "mapping": "by_combo {SOURCE}.{measure} → market_views[].view_id (ML.{SOURCE}.{measure}) allowlist",
        "horizons": {
            "UBIST": {"1y_index": 11, "3y_index": 35, "5y_index": 59},
            "IQVIA": {"1y_index": 3, "3y_index": 11, "5y_index": 19},
        },
        "raw_value_policy": "raw_krw_no_unit_conversion",
    }


def _build_brand_bundle_v1_1(
    brand: str,
    snapshot_at: datetime,
    config: BundleConfig,
    db_conn,
) -> dict:
    brand_context = build_brand_context(brand, db_conn=db_conn)
    market_views = build_market_views(brand_context, snapshot_at.isoformat(), config, db_conn)
    event_bundle = build_event_bundle(brand_context, snapshot_at, config, db_conn)

    competitors_by_source = {}
    for view in market_views:
        source = view["source"]
        if source not in competitors_by_source:
            competitors_by_source[source] = [
                {
                    "brand_name": comp["brand_name"],
                    "rank_in_market": comp.get("rank_in_market"),
                    "is_jw": comp.get("is_jw"),
                }
                for comp in view.get("competitors_top5", [])
            ]
    competitor_events = build_competitor_events(competitors_by_source, snapshot_at, config, db_conn)
    forecast_simulation = _build_forecast_simulation(brand, market_views, config, db_conn)
    bundle = {
        "bundle_meta": {
            "brand": brand,
            "snapshot_at": snapshot_at.isoformat(),
            "config_version": config.config_version,
            "builder_version": config.builder_version,
            "bundle_hash": None,
            "mart_computed_at": _mart_computed_at(db_conn),
            "data_sources_used": {
                "market_metrics": "cache_cause+mart_strategic",
                "ms_computation": "raw_recompute_with_cache_cause_latest",
                "atc4_code": "catalog_strategic_ml_market_fallback_to_mart",
                "competitors": "market_sales_top_n",
                "competitor_events": "event_brand_scores",
                "forecast_simulation": "cache_deep_analysis.simulation.by_combo",
            },
            "available_view_count": len(market_views),
            "stats": {},
        },
        "brand_context": brand_context,
        "market_views": market_views,
        "event_bundle": event_bundle,
        "competitor_events": competitor_events,
        "forecast_simulation": forecast_simulation,
    }
    bundle["bundle_meta"]["stats"] = _compute_stats_v1_1(bundle)
    bundle["bundle_meta"]["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle


def build_brand_bundle(
    brand: str,
    snapshot_at: datetime,
    config: BundleConfig,
    db_conn,
    catalog_path: str = "docs/crawl/_catalog.json",
) -> dict:
    if config.config_version == "phase_zeta_v1_1":
        return _build_brand_bundle_v1_1(brand, snapshot_at, config, db_conn)

    brand_context = build_brand_context(brand, catalog_path)
    market_ids = find_market_ids_for_brand(brand, db_conn, snapshot_at)
    brand_context["market_ids"] = market_ids["ml_ids"]

    market_context = build_market_context(brand, market_ids, db_conn, snapshot_at, config.market)
    event_bundle = build_event_bundle(brand, db_conn, snapshot_at, config.event)
    competitor_context = build_competitor_context(
        brand,
        brand_context["competitors"],
        market_context["primary_market_id"],
        db_conn,
        snapshot_at,
        config.competitor,
    )

    bundle = {
        "bundle_meta": {
            "brand": brand,
            "snapshot_at": snapshot_at.isoformat(),
            "config_version": config.config_version,
            "builder_version": config.builder_version,
            "bundle_hash": None,
            "stats": {},
        },
        "brand_context": brand_context,
        "market_context": market_context,
        "event_bundle": event_bundle,
        "competitor_context": competitor_context,
    }
    bundle["bundle_meta"]["stats"] = _compute_stats(bundle)
    bundle["bundle_meta"]["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle
