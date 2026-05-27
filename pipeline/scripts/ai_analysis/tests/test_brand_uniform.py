from datetime import datetime

from bundle_builder import build_brand_bundle

from .conftest import KST


def test_pilot_brands_same_schema(db_conn, config):
    snapshot = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    bundles = {brand: build_brand_bundle(brand, snapshot, config, db_conn) for brand in config.pilot_brands}

    top_keys = [set(bundle.keys()) for bundle in bundles.values()]
    assert all(keys == top_keys[0] for keys in top_keys), "Top-level keys differ"

    brand_keys = [set(bundle["brand_context"].keys()) for bundle in bundles.values()]
    assert all(keys == brand_keys[0] for keys in brand_keys), "brand_context keys differ"

    market_keys = [set(bundle["market_context"].keys()) for bundle in bundles.values()]
    assert all(keys == market_keys[0] for keys in market_keys), "market_context keys differ"

    metric_keys = [set(bundle["market_context"]["brand_metrics"].keys()) for bundle in bundles.values()]
    assert all(keys == metric_keys[0] for keys in metric_keys), "brand_metrics keys differ"

    market_size_keys = [set(bundle["market_context"]["market_size"].keys()) for bundle in bundles.values()]
    assert all(keys == market_size_keys[0] for keys in market_size_keys), "market_size keys differ"

    event_keys = [set(bundle["event_bundle"].keys()) for bundle in bundles.values()]
    assert all(keys == event_keys[0] for keys in event_keys), "event_bundle keys differ"

    competitor_keys = [set(bundle["competitor_context"].keys()) for bundle in bundles.values()]
    assert all(keys == competitor_keys[0] for keys in competitor_keys), "competitor_context keys differ"
