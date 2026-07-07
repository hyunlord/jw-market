from __future__ import annotations

import json

import pytest

from pipeline.scripts.crawler.tier2_full_scoring_runner import (
    MatchedBrand,
    ParsedTier2Score,
    build_workflow_payload,
    find_workflow_text,
    parse_wf324_response,
    score_tier,
)


def _brands() -> list[MatchedBrand]:
    return [
        MatchedBrand(
            brand_key="brand-a",
            brand_name="프랄런트",
            match_source="body",
            matched_keywords=("프랄런트",),
        ),
        MatchedBrand(
            brand_key="brand-b",
            brand_name="레파타",
            match_source="body",
            matched_keywords=("레파타",),
        ),
    ]


def test_build_payload_uses_target_brands_as_upper_bound() -> None:
    payload = build_workflow_payload(
        news_id="news-1",
        title="PCSK9 시장 경쟁",
        body="프랄런트와 레파타가 함께 언급됐다.",
        source_name="test",
        article_url="https://example.test/a",
        published_date="2026-07-01",
        brands=_brands(),
    )

    assert payload["article"]["news_id"] == "news-1"
    assert [row["brand_key"] for row in payload["target_brands"]] == ["brand-a", "brand-b"]
    assert payload["target_brands"][0]["matched_keywords"] == ["프랄런트"]


def test_find_workflow_text_prefers_data_text_over_echoed_question() -> None:
    raw = {
        "data": {
            "text": "```json\n{\"ok\": true}\n```",
            "agentFlowExecutedData": [
                {"data": {"input": {"question": "{\"echo\": true}"}}},
                {"data": {"output": {"content": "{\"ok\": true}"}}},
            ],
        }
    }

    assert find_workflow_text(raw) == "```json\n{\"ok\": true}\n```"


def test_parse_response_returns_category_and_brand_scores() -> None:
    raw = json.dumps(
        {
            "tag": "신약/R&D",
            "category_label": "신약/R&D",
            "category_code": "rd",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 65, "reason": "직접 비교"},
                {"brand_key": "brand-b", "brand_name": "레파타", "score": 42, "reason": "보조 언급"},
            ],
        },
        ensure_ascii=False,
    )

    parsed = parse_wf324_response(raw, _brands())

    assert parsed.category_label == "신약/R&D"
    assert parsed.category_code == "rd"
    assert parsed.scores == (
        ParsedTier2Score("brand-a", "프랄런트", 65, "직접 비교"),
        ParsedTier2Score("brand-b", "레파타", 42, "보조 언급"),
    )


def test_parse_response_rejects_out_of_candidate_brand() -> None:
    raw = json.dumps(
        {
            "tag": "정책/규제",
            "category_label": "정책/규제",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 70, "reason": "직접"},
                {"brand_key": "brand-x", "brand_name": "후보밖", "score": 60, "reason": "초과"},
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="out-of-candidate"):
        parse_wf324_response(raw, _brands())


def test_parse_response_rejects_missing_candidate_brand() -> None:
    raw = json.dumps(
        {
            "tag": "정책/규제",
            "category_label": "정책/규제",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 70, "reason": "직접"}
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="omitted"):
        parse_wf324_response(raw, _brands())


def test_parse_response_rejects_invalid_category_code_pair() -> None:
    raw = json.dumps(
        {
            "tag": "신약/R&D",
            "category_label": "신약/R&D",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 65, "reason": "직접"},
                {"brand_key": "brand-b", "brand_name": "레파타", "score": 42, "reason": "보조"},
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="category_code"):
        parse_wf324_response(raw, _brands())


def test_score_tier_matches_existing_wf196_thresholds() -> None:
    assert score_tier(0) == "very_weak"
    assert score_tier(30) == "weak"
    assert score_tier(50) == "moderate"
    assert score_tier(70) == "strong"
    assert score_tier(85) == "very_strong"
