from datetime import datetime

from bundle_builder import build_brand_bundle

from .conftest import KST

EXPECTED_TOP_KEYS = {"bundle_meta", "brand_context", "market_context", "event_bundle", "competitor_context"}
EXPECTED_META_KEYS = {"brand", "snapshot_at", "config_version", "builder_version", "bundle_hash", "stats"}
EXPECTED_STATS_KEYS = {
    "event_count_direct",
    "event_count_cross",
    "competitor_count",
    "market_metric_months",
    "estimated_tokens",
}
EXPECTED_BRAND_KEYS = {"name", "english_name", "company", "description", "search_keywords", "market_ids", "competitors"}
EXPECTED_MARKET_KEYS = {"primary_market_id", "market_label", "atc4_code", "brand_metrics", "market_size"}
EXPECTED_EVENT_KEYS = {"direct_events", "cross_match_events", "tag_distribution"}
EXPECTED_COMPETITOR_KEYS = {"competitors"}


def test_top_level_schema(db_conn, config):
    bundle = build_brand_bundle("리바로", datetime(2026, 5, 24, 8, 0, tzinfo=KST), config, db_conn)
    assert set(bundle.keys()) == EXPECTED_TOP_KEYS
    assert set(bundle["bundle_meta"].keys()) == EXPECTED_META_KEYS
    assert set(bundle["bundle_meta"]["stats"].keys()) == EXPECTED_STATS_KEYS
    assert set(bundle["brand_context"].keys()) == EXPECTED_BRAND_KEYS
    assert set(bundle["market_context"].keys()) == EXPECTED_MARKET_KEYS
    assert set(bundle["event_bundle"].keys()) == EXPECTED_EVENT_KEYS
    assert set(bundle["competitor_context"].keys()) == EXPECTED_COMPETITOR_KEYS


def test_metric_keys_are_config_driven(db_conn, config):
    bundle = build_brand_bundle("리바로", datetime(2026, 5, 24, 8, 0, tzinfo=KST), config, db_conn)
    expected = {f"{source}.{measure}" for source, measure in config.market.brand_metrics}
    assert set(bundle["market_context"]["brand_metrics"].keys()) == expected
    assert set(bundle["market_context"]["market_size"].keys()) == expected
