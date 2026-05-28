from __future__ import annotations

from pipeline.scripts.etl import phase29_events


def _row(index: int, *, score: int = 60, brand: str = "플라주오피") -> dict:
    return {
        "event_id": f"event-{index}",
        "news_id": f"news-{index}",
        "brand_name": brand,
        "brand_canonical": brand,
        "score": score,
        "tag": "기타",
        "derivation": "llm_direct",
        "reason": "",
        "mirrored_from_jw_brands": None,
        "event_summary": f"summary {index}",
        "title": f"{brand} event title {index}",
        "summary": f"article summary {index}",
        "body": f"body {index}",
        "source_name": "unit-test",
        "published_date": f"2025-02-{index + 1:02d}",
        "news_url": f"https://example.com/{index}",
        "event_source_url": f"https://example.com/{index}",
    }


def test_cut_a_expands_lookback_until_target_min(monkeypatch) -> None:
    calls: list[tuple[int | None, int]] = []

    def fake_query(conn, brand, *, min_score, lookback_months, limit, derivation=None):
        calls.append((lookback_months, min_score))
        if lookback_months is None and min_score <= 50:
            return [_row(i, score=60) for i in range(5)]
        return []

    monkeypatch.setattr(phase29_events, "_query_events", fake_query)

    events, final_lookback, final_threshold = phase29_events.get_brand_events_cut_a(object(), "플라주오피")

    assert len(events) == 5
    assert final_lookback is None
    assert final_threshold == 50
    assert (6, 0) in calls
    assert (12, 0) in calls
    assert (24, 0) in calls
    assert (None, 50) in calls


def test_cut_a_stops_at_six_months_for_normal_brand(monkeypatch) -> None:
    calls: list[tuple[int | None, int]] = []

    def fake_query(conn, brand, *, min_score, lookback_months, limit, derivation=None):
        calls.append((lookback_months, min_score))
        if lookback_months == 6 and min_score <= 50:
            return [_row(i, score=70, brand="헴리브라") for i in range(5)]
        return []

    monkeypatch.setattr(phase29_events, "_query_events", fake_query)

    events, final_lookback, final_threshold = phase29_events.get_brand_events_cut_a(object(), "헴리브라")

    assert len(events) == 5
    assert final_lookback == 6
    assert final_threshold == 50
    assert calls == [(6, 50)]


def test_cut_a_returns_available_zero_events_after_all_lookbacks(monkeypatch) -> None:
    calls: list[tuple[int | None, int]] = []

    def fake_query(conn, brand, *, min_score, lookback_months, limit, derivation=None):
        calls.append((lookback_months, min_score))
        return []

    monkeypatch.setattr(phase29_events, "_query_events", fake_query)

    events, final_lookback, final_threshold = phase29_events.get_brand_events_cut_a(object(), "없는브랜드")

    assert events == []
    assert final_lookback is None
    assert final_threshold == 0
    assert (6, 0) in calls
    assert (12, 0) in calls
    assert (24, 0) in calls
    assert (None, 0) in calls
