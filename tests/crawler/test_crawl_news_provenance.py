from __future__ import annotations

import json

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
