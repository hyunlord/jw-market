from __future__ import annotations

from pipeline.scripts.crawler.tier2_match_score import (
    Tier2Brand,
    build_tier2_matches,
    candidate_brands_for_item,
    is_ambiguous_brand_name,
    score_exact_match,
)


def test_short_ambiguous_brand_requires_pharma_context() -> None:
    brand = Tier2Brand(brand_name="큐", brand_key="큐", source="ubist")

    no_context = score_exact_match(
        brand,
        title="큐 신규 광고 공개",
        content="큐 브랜드가 새 광고를 공개했다.",
        search_keyword="큐",
    )
    with_context = score_exact_match(
        brand,
        title="큐 처방 확대",
        content="제약 업계에서 큐 처방이 확대됐다.",
        search_keyword="큐",
    )

    assert is_ambiguous_brand_name("큐")
    assert no_context is None
    assert with_context is not None
    assert with_context.score >= 60


def test_exact_brand_match_maps_without_llm() -> None:
    brand = Tier2Brand(brand_name="가드렛", brand_key="가드렛", source="ubist")

    match = score_exact_match(
        brand,
        title="가드렛 임상 결과 발표",
        content="가드렛은 당뇨병 치료제 시장에서 처방 데이터를 발표했다.",
        search_keyword="가드렛",
    )

    assert match is not None
    assert match.brand_name == "가드렛"
    assert match.score >= 70
    assert match.source_processor == "tier2_exact_rule_v1"
    assert "LLM" not in match.reason


def test_multi_brand_mapping_uses_search_keyword_and_exact_text() -> None:
    brands = [
        Tier2Brand(brand_name="가드렛", brand_key="가드렛", source="ubist"),
        Tier2Brand(brand_name="리바로", brand_key="리바로", source="ubist"),
    ]
    item = {
        "title": "리바로 처방 데이터 공개",
        "content": "리바로는 이상지질혈증 치료제다.",
        "search_keyword": "리바로",
    }

    matches = build_tier2_matches(item, brands)

    assert [m["drug"] for m in matches] == ["리바로"]
    assert matches[0]["score"] >= 70


def test_candidate_brands_expand_from_full_article_text() -> None:
    brands = [
        Tier2Brand(brand_name="프랄런트", brand_key="프랄런트", source="ubist"),
        Tier2Brand(brand_name="레파타", brand_key="레파타", source="ubist"),
        Tier2Brand(brand_name="가드렛", brand_key="가드렛", source="ubist"),
    ]
    item = {
        "title": "PCSK9 항체, 무사건 죽상경화성 심혈관질환 환자에도 이득",
        "content": "레파타와 프랄런트는 PCSK9 계열 치료제로 처방 근거가 확대됐다.",
        "search_keyword": "프랄런트",
    }

    candidates = candidate_brands_for_item(item, brands)

    assert [brand.brand_name for brand in candidates] == ["프랄런트", "레파타"]


def test_candidate_brands_use_merged_search_keyword_provenance() -> None:
    brands = [
        Tier2Brand(brand_name="빌로이", brand_key="빌로이", source="ubist"),
        Tier2Brand(brand_name="테빔브라", brand_key="테빔브라", source="ubist"),
    ]
    item = {
        "title": "약평위 통과",
        "content": "위암 치료제와 면역항암제 급여 논의가 이어졌다.",
        "search_keyword": "빌로이",
        "matched_search_keywords": ["빌로이", "테빔브라"],
    }

    candidates = candidate_brands_for_item(item, brands)

    assert [brand.brand_name for brand in candidates] == ["빌로이", "테빔브라"]
