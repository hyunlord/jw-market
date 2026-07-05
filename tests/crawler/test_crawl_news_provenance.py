from __future__ import annotations

import json

from pipeline.scripts.crawler.crawl_news_v2 import merge_keyword_context_for_existing_url


def test_merge_keyword_context_for_existing_url_preserves_tier2_provenance(tmp_path):
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
