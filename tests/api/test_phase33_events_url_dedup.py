from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import re

from pipeline.scripts.etl.phase29_events import format_event


SAMPLE_BRANDS = ["헴리브라", "모빌리아", "가드메트", "페린젝트"]


def _normalize_title(title: str | None) -> str:
    text = (title or "").lower()
    text = re.sub(r"[^\w\s가-힣]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _remaining_duplicate_pairs(events: list[dict], threshold: float = 0.80) -> int:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        groups[(event.get("brand_name") or event.get("brand"), event.get("date"), event.get("category"))].append(event)

    duplicates = 0
    for group in groups.values():
        for left_idx, left in enumerate(group):
            for right in group[left_idx + 1 :]:
                left_title = _normalize_title(left.get("title"))
                right_title = _normalize_title(right.get("title"))
                if left_title and right_title and SequenceMatcher(None, left_title, right_title).ratio() >= threshold:
                    duplicates += 1
    return duplicates


def test_phase33_format_event_exposes_url_fields() -> None:
    event = format_event(
        {
            "event_id": "evt-1",
            "news_id": "news-1",
            "brand_name": "헴리브라",
            "brand_canonical": "헴리브라",
            "score": 88,
            "tag": "신약/R&D",
            "title": "R&D update",
            "summary": "summary",
            "body": "body",
            "source_name": "dailypharm",
            "published_date": "2026-05-26",
            "news_url": "https://example.test/news",
            "event_source_url": "https://example.test/event",
        },
        cut_threshold=50,
    )

    assert event["url"] == "https://example.test/news"
    assert event["source_url"] == "https://example.test/event"


def test_phase33_deep_events_include_urls_and_deduped_clusters(client) -> None:
    total_related_cards = 0

    for brand in SAMPLE_BRANDS:
        response = client.get(f"/api/deep-analysis/{brand}")
        assert response.status_code == 200
        cut_a = response.json()["data"]["events"]["cut_a"]
        assert cut_a, brand

        url_events = [event for event in cut_a if event.get("url")]
        assert len(url_events) == len(cut_a), brand
        assert all("source_url" in event for event in cut_a), brand

        duplicate_pairs = _remaining_duplicate_pairs(cut_a)
        assert duplicate_pairs == 0, f"{brand} has {duplicate_pairs} remaining duplicate title pairs"

        total_related_cards += sum(1 for event in cut_a if event.get("related_coverage_count", 1) > 1)

    assert total_related_cards > 0
