from __future__ import annotations

import json

import pytest

from pipeline.scripts.crawler.tier2_llm_tagging import build_tier2_llm_payload, parse_tier2_llm_response
from pipeline.scripts.crawler.tier2_match_score import Tier2Brand


def test_build_payload_uses_candidates_as_upper_bound() -> None:
    item = {
        "title": "PCSK9 항체 급여 논의",
        "content": "프랄런트와 레파타가 함께 언급됐다.",
        "search_keyword": "프랄런트",
        "matched_search_keywords": ["레파타"],
    }
    candidates = [
        Tier2Brand("프랄런트", "brand-a", "ubist", "C10A1"),
        Tier2Brand("레파타", "brand-b", "ubist", "C10A1"),
    ]

    payload = build_tier2_llm_payload(item, candidates)

    assert payload["article"]["search_keywords"] == ["프랄런트", "레파타"]
    assert [row["brand_key"] for row in payload["candidates"]] == ["brand-a", "brand-b"]


def test_parse_response_rejects_out_of_candidate_brand() -> None:
    candidates = [Tier2Brand("프랄런트", "brand-a", "ubist")]
    raw = json.dumps(
        {
            "candidates": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "include": True, "relevance_score": 80, "reason": "직접 언급"},
                {"brand_key": "brand-x", "brand_name": "레파타", "include": True, "relevance_score": 70, "reason": "후보 밖"},
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="out-of-candidate"):
        parse_tier2_llm_response(raw, candidates)


def test_parse_response_returns_processor_policy() -> None:
    candidates = [Tier2Brand("프랄런트", "brand-a", "ubist")]
    raw = json.dumps(
        {
            "candidates": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "include": True, "relevance_score": 80, "reason": "직접 언급"}
            ]
        },
        ensure_ascii=False,
    )

    [decision] = parse_tier2_llm_response(raw, candidates)

    assert decision.source_processor == "tier2_llm_v1"
    assert decision.include is True
    assert decision.relevance_score == 80
