from __future__ import annotations

import json

from pipeline.scripts.crawler.tier2_body_match_runner import (
    STOPLIST_BRAND_NAMES,
    BodyMatchBrand,
    BodyMatchRunnerConfig,
    Tier2BodyMatcher,
    should_pause_for_ambiguous_brand,
)


def test_longest_match_keeps_nested_long_brand_only() -> None:
    matcher = Tier2BodyMatcher(
        [
            BodyMatchBrand(brand_key="livaro", brand_name="리바로", source="ubist"),
            BodyMatchBrand(brand_key="livarozet", brand_name="리바로젯", source="ubist"),
        ],
        BodyMatchRunnerConfig(),
    )

    matches = matcher.match_news(
        news_id="n1",
        title="리바로젯 처방 확대",
        article_text="리바로젯은 이상지질혈증 치료제로 언급됐다.",
        collection_provenance="[]",
    )

    assert [(item.brand_key, item.brand_name) for item in matches] == [("livarozet", "리바로젯")]


def test_stoplist_short_brand_requires_tier2_search_provenance() -> None:
    matcher = Tier2BodyMatcher(
        [BodyMatchBrand(brand_key="zero", brand_name="제로", source="ubist")],
        BodyMatchRunnerConfig(),
    )

    no_provenance = matcher.match_news(
        news_id="n1",
        title="제로 성장률 전망",
        article_text="시장 성장률은 제로에 가까웠다.",
        collection_provenance="[]",
    )
    with_provenance = matcher.match_news(
        news_id="n2",
        title="제로 성장률 전망",
        article_text="시장 성장률은 제로에 가까웠다.",
        collection_provenance=json.dumps(
            [
                {
                    "tier": 2,
                    "brand": "제로",
                    "brand_key": "zero",
                    "matched_keywords": ["제로"],
                }
            ],
            ensure_ascii=False,
        ),
    )

    assert "제로" in STOPLIST_BRAND_NAMES
    assert no_provenance == []
    assert len(with_provenance) == 1
    assert with_provenance[0].match_source == "body+search_provenance"


def test_three_syllable_brand_is_paused_without_provenance() -> None:
    assert should_pause_for_ambiguous_brand("파인")
    assert not should_pause_for_ambiguous_brand("리쥬비넥스")


def test_metric_like_unknown_numbers_are_not_relevant_to_rule_match() -> None:
    matcher = Tier2BodyMatcher(
        [BodyMatchBrand(brand_key="careplus", brand_name="케어플러스", source="ubist")],
        BodyMatchRunnerConfig(),
    )

    matches = matcher.match_news(
        news_id="n3",
        title="케어플러스 매출 69,753,578원",
        article_text="케어플러스는 본문에 정확히 등장한다.",
        collection_provenance="[]",
    )

    assert [(item.brand_key, item.matched_keywords) for item in matches] == [
        ("careplus", ("케어플러스",))
    ]
