from __future__ import annotations

from pipeline.scripts.etl.phase29_events import (
    _filter_cut_b_rows,
    _filter_news_exposure_rows,
    format_event,
)


def test_news_and_cut_b_filters_share_processor_policy() -> None:
    rows = [
        {"id": "legacy-news", "tag": "자본/경영", "score": 43, "source_processor": None},
        {"id": "new-news-low", "tag": "자본/경영", "score": 52, "source_processor": "workflow_196_rev5674"},
        {"id": "new-news-edge", "tag": "자본/경영", "score": 53, "source_processor": "workflow_196_rev5674"},
        {"id": "other", "tag": "기타", "score": 100, "source_processor": None},
    ]

    news = _filter_news_exposure_rows(rows)

    assert [row["id"] for row in news] == ["legacy-news", "new-news-edge"]

    cut_b_rows = [
        {"id": "legacy-80", "tag": "자본/경영", "score": 80, "source_processor": None},
        {"id": "new-87", "tag": "자본/경영", "score": 87, "source_processor": "workflow_196_rev5674"},
        {"id": "new-88", "tag": "자본/경영", "score": 88, "source_processor": "workflow_196_rev5674"},
    ]

    cut_b = _filter_cut_b_rows(cut_b_rows)

    assert [row["id"] for row in cut_b] == ["legacy-80", "new-88"]


def test_formatted_event_keeps_effective_cut_threshold_without_internal_processor() -> None:
    row = {
        "event_id": "e1",
        "news_id": "n1",
        "brand_name": "Brand",
        "score": 88,
        "tag": "신약/R&D",
        "source_processor": "workflow_196_rev5674",
    }

    event = format_event(row, cut_threshold=88)

    assert event["cut_threshold"] == 88
    assert "source_processor" not in event
