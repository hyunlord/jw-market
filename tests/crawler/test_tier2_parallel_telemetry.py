from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pipeline.scripts.crawler import crawl_2tier
from pipeline.scripts.crawler import crawl_news_v2 as crawler
from pipeline.scripts.crawler.tier2_match_score import Tier2Brand


def _tier2_args(tmp_path, *, sites: str | None = None, concurrent_sites: int = 4):
    return SimpleNamespace(
        output_dir=str(tmp_path / "raw"),
        days=7,
        max_pages_per_site=3,
        max_links_per_page=80,
        delay_sec=5,
        sites=sites,
        no_similar_merge=True,
        unique_json_per_url=True,
        max_articles=0,
        concurrent_sites=concurrent_sites,
        telemetry_file=str(tmp_path / "tier2_crawl_telemetry.json"),
    )


def _brand() -> Tier2Brand:
    return Tier2Brand(
        brand_name="테스트브랜드",
        brand_key="test_brand",
        source="test",
    )


def test_tier2_parallel_runner_isolates_site_failure_and_serializes_each_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(crawl_2tier, "_import_crawler", lambda: crawler)
    monkeypatch.setattr(
        crawler,
        "SITE_CONFIGS",
        {"사이트A": {}, "사이트B": {}, "사이트C": {}},
    )
    active: dict[str, int] = {}
    max_active: dict[str, int] = {}
    completed: list[str] = []
    lock = threading.Lock()

    def fake_crawl_once(**kwargs: object) -> int:
        site = kwargs["sites"][0]  # type: ignore[index]
        with lock:
            active[site] = active.get(site, 0) + 1
            max_active[site] = max(max_active.get(site, 0), active[site])
        time.sleep(0.02)
        try:
            if site == "사이트B":
                raise RuntimeError("injected site failure")
            completed.append(site)
            return 1
        finally:
            with lock:
                active[site] -= 1

    monkeypatch.setattr(crawler, "crawl_once", fake_crawl_once)

    with pytest.raises(crawl_2tier.Tier2SiteCrawlError, match="사이트B"):
        crawl_2tier.run_tier2_crawl(_tier2_args(tmp_path), [_brand()])
    assert sorted(completed) == ["사이트A", "사이트C"]
    assert max(max_active.values()) == 1

    telemetry = json.loads(
        (tmp_path / "tier2_crawl_telemetry.json").read_text(encoding="utf-8")
    )
    assert telemetry["sites"]["사이트B"]["errors"] == 1
    assert telemetry["totals"]["site_errors"] == 1


def test_duplicate_site_selection_is_rejected_before_parallel_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        crawl_2tier,
        "_import_crawler",
        lambda: SimpleNamespace(SITE_CONFIGS={"사이트A": {}, "사이트B": {}}),
    )

    with pytest.raises(ValueError, match="duplicate tier2 site"):
        crawl_2tier.tier2_sites("사이트A,사이트A")


def test_max_articles_preserves_one_global_bound_across_sites(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(crawl_2tier, "_import_crawler", lambda: crawler)
    monkeypatch.setattr(
        crawler,
        "SITE_CONFIGS",
        {"사이트A": {}, "사이트B": {}},
    )
    calls: list[dict[str, object]] = []

    def fake_crawl_once(**kwargs: object) -> int:
        calls.append(kwargs)
        return int(kwargs["max_articles"])

    monkeypatch.setattr(crawler, "crawl_once", fake_crawl_once)
    args = _tier2_args(tmp_path, sites="사이트A,사이트B")
    args.max_articles = 3

    assert crawl_2tier.run_tier2_crawl(args, [_brand()]) == 3
    assert len(calls) == 1
    assert calls[0]["sites"] == ["사이트A", "사이트B"]
    assert calls[0]["max_articles"] == 3


def test_history_ledger_concurrent_append_has_no_duplicates_or_loss(tmp_path) -> None:
    history_file = tmp_path / "scraped_urls.txt"
    ledger = crawler.ScrapedUrlLedger(str(history_file))
    urls = [f"https://example.test/{index}" for index in range(100)]

    with ThreadPoolExecutor(max_workers=11) as pool:
        results = list(pool.map(ledger.append, urls + urls))

    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert sum(results) == 100
    assert len(lines) == 100
    assert len(set(lines)) == 100
    assert set(lines) == set(urls)


def test_telemetry_is_durable_and_reports_latency_distribution(tmp_path) -> None:
    telemetry_path = tmp_path / "tier2_crawl_telemetry.json"
    telemetry = crawler.CrawlTelemetry(str(telemetry_path))

    for elapsed in (0.1, 0.2, 0.3, 0.4, 1.0):
        telemetry.record_request("사이트A", "detail", elapsed)
    telemetry.record_sleep("사이트A", "detail", 5.0)
    telemetry.record_count("사이트A", "detail", "dedupe_skipped", 3)
    telemetry.finalize()

    report = json.loads(telemetry_path.read_text(encoding="utf-8"))
    events = telemetry_path.with_suffix(".events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    detail = report["sites"]["사이트A"]["detail"]
    assert len(events) == 7
    assert detail["requests"] == 5
    assert detail["sleep_sec"] == 5.0
    assert detail["dedupe_skipped"] == 3
    assert detail["response_sec"]["p50"] == 0.3
    assert detail["response_sec"]["p90"] == 1.0
    assert detail["response_sec"]["max"] == 1.0


def test_parallel_crawl_preserves_prefetch_dedupe_and_pacing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    existing_url = "https://example.test/existing"
    fresh_url = "https://example.test/fresh"
    history_file = tmp_path / "scraped_urls.txt"
    history_file.write_text(f"{existing_url}\n", encoding="utf-8")
    ledger = crawler.ScrapedUrlLedger(str(history_file))
    telemetry = crawler.CrawlTelemetry(str(tmp_path / "telemetry.json"))
    fetch_calls: list[str] = []
    sleep_calls: list[float] = []

    monkeypatch.setattr(crawler, "SITE_CONFIGS", {"사이트A": {}})
    monkeypatch.setattr(
        crawler,
        "iter_site_paginated_configs",
        lambda *_args, **_kwargs: [({}, 1)],
    )
    monkeypatch.setattr(
        crawler,
        "get_article_links",
        lambda *_args, **_kwargs: [
            (existing_url, "기존 기사"),
            (fresh_url, "신규 기사"),
        ],
    )

    def fake_fetch(url: str) -> str:
        fetch_calls.append(url)
        return "<html>article</html>"

    monkeypatch.setattr(crawler, "fetch_html_requests", fake_fetch)
    monkeypatch.setattr(
        crawler,
        "extract_news_content",
        lambda *_args, **_kwargs: {
            "title": "오래된 기사",
            "date": "2000-01-01",
            "content": "본문",
        },
    )
    monkeypatch.setattr(crawler.time, "sleep", sleep_calls.append)

    saved = crawler.crawl_once(
        months=None,
        days=7,
        output_dir=str(tmp_path / "out"),
        max_pages_per_site=1,
        max_links_per_page=10,
        delay_sec=5,
        sites=["사이트A"],
        keywords=["테스트"],
        history_file=str(history_file),
        history_ledger=ledger,
        telemetry=telemetry,
    )
    telemetry.finalize()

    assert saved == 0
    assert fetch_calls == [fresh_url]
    assert sleep_calls == [5]
    assert history_file.read_text(encoding="utf-8").splitlines() == [
        existing_url,
        fresh_url,
    ]
    report = telemetry.snapshot()
    assert report["sites"]["사이트A"]["detail"]["dedupe_skipped"] == 1
    assert report["sites"]["사이트A"]["detail"]["sleep_sec"] == 5.0


def test_pacing_contract_detects_removed_sleep_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    telemetry = crawler.CrawlTelemetry(str(tmp_path / "telemetry.json"))
    sleep_calls: list[float] = []
    monkeypatch.setattr(crawler.time, "sleep", sleep_calls.append)

    crawler._paced_sleep("사이트A", 5.0, telemetry)

    assert sleep_calls == [5.0]
    assert telemetry.snapshot()["sites"]["사이트A"]["detail"]["sleep_sec"] == 5.0
