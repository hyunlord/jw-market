from datetime import datetime

import pytest

from bundle_builder.brand_context_builder import build_brand_context, find_market_ids_for_brand
from bundle_builder.competitor_context_builder import build_competitor_context
from bundle_builder.event_bundle_builder import build_event_bundle

from .conftest import KST


def test_brand_context_riva():
    bc = build_brand_context("리바로")
    assert bc["name"] == "리바로"
    assert bc["competitors"] == ["리바로젯", "리피로우", "아토르바", "크레스토"]
    assert "LIVALO" in str(bc.get("search_keywords", {}))


def test_brand_context_missing_brand():
    with pytest.raises(ValueError):
        build_brand_context("존재하지않는브랜드")


def test_market_ids_for_riva(db_conn):
    ids = find_market_ids_for_brand("리바로", db_conn, datetime(2026, 5, 24, 8, 0, tzinfo=KST))
    assert "ml_006" in ids["ml_ids"]


def test_event_cutoff_applied(db_conn, config):
    eb = build_event_bundle("리바로", db_conn, datetime(2026, 5, 24, 8, 0, tzinfo=KST), config.event)
    assert all(e["score"] >= config.event.min_score_direct for e in eb["direct_events"])
    assert len(eb["direct_events"]) <= config.event.max_count_direct
    rows = [(e["score"], e["published_date"], e["news_id"]) for e in eb["direct_events"]]
    for previous, current in zip(rows, rows[1:]):
        assert previous[0] >= current[0]
        if previous[0] == current[0]:
            assert previous[1] >= current[1]
        if previous[0] == current[0] and previous[1] == current[1]:
            assert previous[2] <= current[2]


def test_cross_match_uses_mirrored_from_brand(db_conn, config):
    eb = build_event_bundle("라베칸듀오", db_conn, datetime(2026, 5, 24, 8, 0, tzinfo=KST), config.event)
    assert eb["cross_match_events"], "expected mirrored cross-match events for 라베칸듀오"
    assert all("라베칸듀오" in e["mirrored_from"] for e in eb["cross_match_events"])


def test_competitor_context_riva(db_conn, config):
    bc = build_brand_context("리바로")
    cc = build_competitor_context(
        "리바로",
        bc["competitors"],
        "ml_006",
        db_conn,
        datetime(2026, 5, 24, 8, 0, tzinfo=KST),
        config.competitor,
    )
    assert [c["name"] for c in cc["competitors"]] == ["리바로젯", "리피로우", "아토르바", "크레스토"]
