from __future__ import annotations

from datetime import datetime

from .brand_context_builder import build_brand_context, find_market_ids_for_brand
from .competitor_events_builder import build_competitor_events
from .competitor_context_builder import build_competitor_context
from .config import BundleConfig
from .event_bundle_builder import build_event_bundle
from .hash_util import compute_bundle_hash, deterministic_json_dumps
from .market_context_builder import build_market_context
from .market_views_orchestrator import build_market_views


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
            },
            "available_view_count": len(market_views),
            "stats": {},
        },
        "brand_context": brand_context,
        "market_views": market_views,
        "event_bundle": event_bundle,
        "competitor_events": competitor_events,
        "forecast_simulation": _forecast_placeholder(),
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
