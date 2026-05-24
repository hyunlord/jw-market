from __future__ import annotations

from datetime import datetime

from .brand_context_builder import build_brand_context, find_market_ids_for_brand
from .competitor_context_builder import build_competitor_context
from .config import BundleConfig
from .event_bundle_builder import build_event_bundle
from .hash_util import compute_bundle_hash, deterministic_json_dumps
from .market_context_builder import build_market_context


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


def build_brand_bundle(
    brand: str,
    snapshot_at: datetime,
    config: BundleConfig,
    db_conn,
    catalog_path: str = "docs/crawl/_catalog.json",
) -> dict:
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
