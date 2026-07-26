from __future__ import annotations

import json

from pipeline.scripts.crawler import crawl_news_v2 as crawler
from pipeline.scripts.crawler.crawl_news_v2 import merge_keyword_context_for_existing_url


def test_merge_keyword_context_for_existing_url_preserves_jw_brand_context_shape(tmp_path):
    article = tmp_path / "article.json"
    article.write_text(
        json.dumps(
            {
                "title": "동일 기사",
                "date": "2026-07-05",
                "content": "본문",
                "sources": [{"source": "테스트", "url": "https://example.test/a"}],
                "matched_search_keywords": ["프랄런트"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    merged = merge_keyword_context_for_existing_url(
        str(tmp_path),
        "테스트",
        "https://example.test/a",
        "레파타",
        keyword_contexts={
            "레파타": [
                {"jw_brand": "레파타", "matched_keywords": ["레파타"]},
            ]
        },
    )

    doc = json.loads(article.read_text(encoding="utf-8"))
    assert merged is True
    assert doc["matched_search_keywords"] == ["레파타", "프랄런트"]
    assert doc["matched_jw_search_contexts"] == [
        {"jw_brand": "레파타", "matched_keywords": ["레파타"]}
    ]


def test_merge_keyword_context_for_existing_url_preserves_tier2_context_without_jw_brand(tmp_path):
    article = tmp_path / "article.json"
    article.write_text(
        json.dumps(
            {
                "title": "동일 기사",
                "date": "2026-07-05",
                "content": "본문",
                "sources": [{"source": "테스트", "url": "https://example.test/a"}],
                "matched_search_keywords": ["프랄런트"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    merged = merge_keyword_context_for_existing_url(
        str(tmp_path),
        "테스트",
        "https://example.test/a",
        "레파타",
        keyword_contexts={
            "레파타": [
                {
                    "tier": 2,
                    "brand_key": "repatha_key",
                    "source": "tier2_catalog",
                    "matched_keywords": ["레파타"],
                },
            ]
        },
    )

    doc = json.loads(article.read_text(encoding="utf-8"))
    expected_context = {
        "tier": 2,
        "brand": "레파타",
        "brand_key": "repatha_key",
        "source": "tier2_catalog",
        "matched_keywords": ["레파타"],
    }
    assert merged is True
    assert doc["matched_search_keywords"] == ["레파타", "프랄런트"]
    assert doc["matched_jw_search_contexts"] == [expected_context]
    assert doc["collection_provenance"] == [expected_context]


def test_merge_keyword_context_for_existing_url_unions_tier1_and_tier2_contexts(tmp_path):
    article = tmp_path / "article.json"
    article.write_text(
        json.dumps(
            {
                "title": "동일 기사",
                "date": "2026-07-05",
                "content": "본문",
                "sources": [{"source": "테스트", "url": "https://example.test/a"}],
                "matched_search_keywords": ["리바로"],
                "matched_jw_search_contexts": [
                    {"jw_brand": "리바로", "matched_keywords": ["리바로"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    merged = merge_keyword_context_for_existing_url(
        str(tmp_path),
        "테스트",
        "https://example.test/a",
        "레파타",
        keyword_contexts={
            "레파타": [
                {
                    "tier": 2,
                    "brand_key": "repatha_key",
                    "source": "tier2_catalog",
                    "matched_keywords": ["레파타"],
                },
            ]
        },
    )

    doc = json.loads(article.read_text(encoding="utf-8"))
    assert merged is True
    assert doc["matched_search_keywords"] == ["레파타", "리바로"]
    assert doc["matched_jw_search_contexts"] == [
        {"jw_brand": "리바로", "matched_keywords": ["리바로"]},
        {
            "tier": 2,
            "brand": "레파타",
            "brand_key": "repatha_key",
            "source": "tier2_catalog",
            "matched_keywords": ["레파타"],
        },
    ]


def test_merge_keyword_context_for_existing_url_keeps_flat_keyword_only_without_contexts(tmp_path):
    article = tmp_path / "article.json"
    article.write_text(
        json.dumps(
            {
                "title": "동일 기사",
                "date": "2026-07-05",
                "content": "본문",
                "sources": [{"source": "테스트", "url": "https://example.test/a"}],
                "matched_search_keywords": ["프랄런트"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    merged = merge_keyword_context_for_existing_url(
        str(tmp_path),
        "테스트",
        "https://example.test/a",
        "레파타",
    )

    doc = json.loads(article.read_text(encoding="utf-8"))
    assert merged is True
    assert doc["matched_search_keywords"] == ["레파타", "프랄런트"]
    assert "matched_jw_search_contexts" not in doc
    assert "collection_provenance" not in doc


def _stub_single_article_crawl(monkeypatch, url: str, fetch_calls: list[str]) -> None:
    monkeypatch.setattr(crawler, "SITE_CONFIGS", {"테스트": {}})
    monkeypatch.setattr(
        crawler,
        "iter_site_paginated_configs",
        lambda *_args, **_kwargs: [({}, 1)],
    )
    monkeypatch.setattr(
        crawler,
        "get_article_links",
        lambda *_args, **_kwargs: [(url, "기사")],
    )

    def fetch_html(requested_url: str) -> str:
        fetch_calls.append(requested_url)
        return "<html>article</html>"

    monkeypatch.setattr(crawler, "fetch_html_requests", fetch_html)
    monkeypatch.setattr(
        crawler,
        "extract_news_content",
        lambda *_args, **_kwargs: {
            "title": "오래된 기사",
            "date": "2000-01-01",
            "content": "본문",
        },
    )


def test_crawl_once_skips_article_get_for_url_already_in_history(
    monkeypatch,
    tmp_path,
):
    url = "https://example.test/already-processed"
    history_file = tmp_path / "scraped_urls.txt"
    history_file.write_text(f"{url}\n", encoding="utf-8")
    fetch_calls: list[str] = []
    _stub_single_article_crawl(monkeypatch, url, fetch_calls)

    saved = crawler.crawl_once(
        months=None,
        days=1,
        output_dir=str(tmp_path / "out"),
        max_pages_per_site=1,
        max_links_per_page=10,
        delay_sec=5,
        keywords=["테스트"],
        history_file=str(history_file),
    )

    assert saved == 0
    assert fetch_calls == []


def test_crawl_once_persists_old_url_before_sleep_and_skips_it_next_run(
    monkeypatch,
    tmp_path,
):
    url = "https://example.test/old-article"
    history_file = tmp_path / "scraped_urls.txt"
    fetch_calls: list[str] = []
    sleep_calls: list[float] = []
    _stub_single_article_crawl(monkeypatch, url, fetch_calls)
    monkeypatch.setattr(crawler.time, "sleep", sleep_calls.append)

    first_saved = crawler.crawl_once(
        months=None,
        days=1,
        output_dir=str(tmp_path / "out"),
        max_pages_per_site=1,
        max_links_per_page=10,
        delay_sec=5,
        keywords=["테스트"],
        history_file=str(history_file),
    )

    assert first_saved == 0
    assert fetch_calls == [url]
    assert history_file.read_text(encoding="utf-8").splitlines() == [url]
    assert sleep_calls == [5]

    fetch_calls.clear()
    second_saved = crawler.crawl_once(
        months=None,
        days=1,
        output_dir=str(tmp_path / "out"),
        max_pages_per_site=1,
        max_links_per_page=10,
        delay_sec=5,
        keywords=["테스트"],
        history_file=str(history_file),
    )

    assert second_saved == 0
    assert fetch_calls == []
