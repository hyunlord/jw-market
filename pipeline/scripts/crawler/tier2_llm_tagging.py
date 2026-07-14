"""Tier2 LLM candidate-confirmation helpers.

The deterministic Tier2 scorer finds possible brands from search provenance
and exact article text. The LLM workflow confirms only those candidates; it
does not discover new brands. Persisted evidence from this path uses
``tier2_llm_v1`` while rule-only provenance remains ``tier2_exact_rule_v1``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pipeline.scripts.crawler.tier2_match_score import TIER2_LLM_PROCESSOR, Tier2Brand, item_search_keywords


@dataclass(frozen=True)
class Tier2LlmDecision:
    brand_key: str
    brand_name: str
    include: bool
    relevance_score: int
    reason: str
    source_processor: str = TIER2_LLM_PROCESSOR


def build_tier2_llm_payload(item: dict[str, Any], candidates: list[Tier2Brand]) -> dict[str, Any]:
    """Build the workflow input with candidates as a hard upper bound."""
    return {
        "article": {
            "title": str(item.get("title") or ""),
            "content": str(item.get("content") or item.get("article_text") or ""),
            "source_name": item.get("source_name") or item.get("source"),
            "search_keywords": list(item_search_keywords(item)),
        },
        "candidates": [
            {
                "brand_key": brand.brand_key,
                "brand_name": brand.brand_name,
                "source": brand.source,
                "atc4_code": brand.atc4_code,
            }
            for brand in candidates
        ],
    }


def strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


def parse_tier2_llm_response(raw_text: str, candidates: list[Tier2Brand]) -> list[Tier2LlmDecision]:
    """Parse and validate a tier2_llm_v1 response against the candidate set."""
    payload = json.loads(strip_json_fence(raw_text))
    rows = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("tier2_llm_v1 response must contain a candidates list")

    allowed = {brand.brand_key: brand for brand in candidates}
    seen: set[str] = set()
    decisions: list[Tier2LlmDecision] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("tier2_llm_v1 candidate item must be an object")
        brand_key = str(row.get("brand_key") or "").strip()
        if brand_key not in allowed:
            raise ValueError(f"tier2_llm_v1 returned out-of-candidate brand_key={brand_key!r}")
        if brand_key in seen:
            raise ValueError(f"tier2_llm_v1 returned duplicate brand_key={brand_key!r}")
        seen.add(brand_key)
        score = int(row.get("relevance_score") or 0)
        decisions.append(
            Tier2LlmDecision(
                brand_key=brand_key,
                brand_name=str(row.get("brand_name") or allowed[brand_key].brand_name).strip(),
                include=bool(row.get("include")),
                relevance_score=max(0, min(100, score)),
                reason=str(row.get("reason") or "").strip(),
            )
        )
    missing = set(allowed) - seen
    if missing:
        raise ValueError(f"tier2_llm_v1 omitted candidate brand_key(s): {sorted(missing)}")
    return decisions
