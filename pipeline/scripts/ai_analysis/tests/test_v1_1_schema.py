import re
from datetime import datetime

from bundle_builder import build_brand_bundle, render_narrative

from .conftest import KST


def _bundle(brand, db_conn, config_v1_1):
    return build_brand_bundle(brand, datetime(2026, 5, 25, 8, 0, tzinfo=KST), config_v1_1, db_conn)


def test_top_level_v1_1_keys(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    expected = {
        "bundle_meta",
        "brand_context",
        "market_views",
        "event_bundle",
        "competitor_events",
        "forecast_simulation",
    }
    assert set(bundle.keys()) == expected


def test_market_views_is_list(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    assert isinstance(bundle["market_views"], list)
    assert len(bundle["market_views"]) > 0


def test_market_view_schema(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    for view in bundle["market_views"]:
        assert set(view.keys()) == {
            "view_id",
            "view",
            "source",
            "measure",
            "market_meta",
            "market_size",
            "target_brand_metric",
            "competitors_top5",
            "channel_breakdown",
        }
        assert len(view["competitors_top5"]) == 5


def test_competitors_target_excluded(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    for view in bundle["market_views"]:
        names = [c["brand_name"] for c in view["competitors_top5"]]
        assert "리바로" not in names


def test_event_bundle_dedup(db_conn, config_v1_1):
    bundle = _bundle("헴리브라", db_conn, config_v1_1)
    events = bundle["event_bundle"]["events_brand_centric"] + bundle["event_bundle"]["events_market_trend"]
    seen_dates = set()
    for event in events:
        assert event["published_date"] not in seen_dates
        seen_dates.add(event["published_date"])


def test_brand_centric_classification(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    for event in bundle["event_bundle"]["events_brand_centric"]:
        haystack = f"{event['title']} {event['summary']}"
        assert "리바로" in haystack or "LIVALO" in haystack


def test_forecast_simulation_placeholder(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    assert bundle["forecast_simulation"]["available"] is False


def test_dual_brand_has_both_source_competitors(db_conn, config_v1_1):
    bundle = _bundle("가드메트", db_conn, config_v1_1)
    assert {"UBIST", "IQVIA"} <= set(bundle["competitor_events"]["by_source"])


def test_single_source_brand_only_one(db_conn, config_v1_1):
    bundle = _bundle("헴리브라", db_conn, config_v1_1)
    assert set(bundle["competitor_events"]["by_source"]) == {"IQVIA"}


def test_atc4_code_populated(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    assert bundle["brand_context"]["atc4_code"] == "C10A1"


def test_mat_12m_absolute_computed(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    for view in bundle["market_views"]:
        mat = view["target_brand_metric"]["mat_12m_absolute"]
        assert mat["latest_period"] is not None
        assert mat["value"] is not None


def test_no_unit_conversion_in_narrative(db_conn, config_v1_1):
    bundle = _bundle("리바로", db_conn, config_v1_1)
    narrative = render_narrative(bundle)
    assert not re.findall(r"\d+(?:\.\d+)?\s*억", narrative)


def test_expected_view_counts_from_cache(db_conn, config_v1_1):
    hem = _bundle("헴리브라", db_conn, config_v1_1)
    rab = _bundle("라베칸", db_conn, config_v1_1)
    assert hem["bundle_meta"]["available_view_count"] == 8
    assert rab["bundle_meta"]["available_view_count"] == 4
