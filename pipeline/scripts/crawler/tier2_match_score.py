"""Tier2 exact-match mapping and zero-LLM relevance scoring.

Tier2 intentionally avoids workflow-196 brand classification. A crawled
article is mapped to the brand that was searched only when the brand name is
present as an exact phrase. Short or generic names also require pharmaceutical
context terms so there are no brand-specific exception lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


PHARMA_CONTEXT_TERMS: tuple[str, ...] = (
    "제약",
    "처방",
    "임상",
    "약",
    "의약",
    "식약처",
    "병원",
    "환자",
    "치료",
    "급여",
)

COMMON_GENERIC_NAMES: frozenset[str] = frozenset(
    {
        "큐",
        "원",
        "온",
        "맥스",
        "엠",
        "정",
        "캡",
        "탑",
        "케이",
        "plus",
        "max",
        "one",
        "on",
    }
)

TIER2_RULE_PROCESSOR = "tier2_exact_rule_v1"
TIER2_LLM_PROCESSOR = "tier2_llm_v1"


@dataclass(frozen=True)
class Tier2Brand:
    brand_name: str
    brand_key: str
    source: str
    atc4_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Tier2Match:
    brand_name: str
    brand_key: str
    score: int
    score_tier: str
    reason: str
    source_processor: str = TIER2_RULE_PROCESSOR

    def as_score_match(self) -> dict[str, Any]:
        return {
            "drug": self.brand_name,
            "brand_key": self.brand_key,
            "brand_canonical": self.brand_key,
            "score": self.score,
            "score_tier": self.score_tier,
            "reason": self.reason,
            "source_processor": self.source_processor,
        }


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def is_ambiguous_brand_name(brand_name: str) -> bool:
    text = normalize_text(brand_name)
    if not text:
        return True
    compact = re.sub(r"\s+", "", text)
    if compact in COMMON_GENERIC_NAMES:
        return True
    if re.fullmatch(r"[a-z0-9]+", compact) and len(compact) <= 3:
        return True
    korean_chars = re.findall(r"[가-힣]", compact)
    return 0 < len(korean_chars) <= 2


def has_exact_phrase(text: str, phrase: str) -> bool:
    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_phrase:
        return False
    if re.fullmatch(r"[a-z0-9]+", normalized_phrase):
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])", normalized_text) is not None
    return normalized_phrase in normalized_text


def has_pharma_context(title: str, content: str) -> bool:
    haystack = f"{title} {content}"
    return any(term in haystack for term in PHARMA_CONTEXT_TERMS)


def score_to_tier(score: int) -> str:
    if score >= 85:
        return "tier2_primary"
    if score >= 70:
        return "tier2_relevant"
    if score >= 55:
        return "tier2_contextual"
    if score >= 35:
        return "tier2_mention"
    return "excluded"


def score_exact_match(
    brand: Tier2Brand,
    *,
    title: str,
    content: str,
    search_keyword: str | None = None,
    search_keywords: Iterable[str] | None = None,
) -> Tier2Match | None:
    brand_name = brand.brand_name.strip()
    if not brand_name:
        return None
    title_hit = has_exact_phrase(title, brand_name)
    content_hit = has_exact_phrase(content, brand_name)
    keyword_values = list(search_keywords or ())
    if search_keyword:
        keyword_values.append(search_keyword)
    normalized_brand = normalize_text(brand_name)
    keyword_hit = any(normalize_text(keyword) == normalized_brand for keyword in keyword_values)
    if not (title_hit or content_hit or keyword_hit):
        return None
    if is_ambiguous_brand_name(brand_name) and not has_pharma_context(title, content):
        return None

    score = 30
    evidence: list[str] = []
    if title_hit:
        score += 35
        evidence.append("title exact")
    if content_hit:
        mentions = len(re.findall(re.escape(normalize_text(brand_name)), normalize_text(content)))
        score += min(25, 8 + mentions * 4)
        evidence.append(f"body exact x{mentions}")
    if keyword_hit:
        score += 15
        evidence.append("searched brand")
    if has_pharma_context(title, content):
        score += 10
        evidence.append("pharma context")

    bounded_score = max(10, min(95, score))
    return Tier2Match(
        brand_name=brand.brand_name,
        brand_key=brand.brand_key,
        score=bounded_score,
        score_tier=score_to_tier(bounded_score),
        reason="Tier2 exact-match rule: " + ", ".join(evidence),
    )


def item_search_keywords(item: dict[str, Any]) -> tuple[str, ...]:
    """Return all search-provenance keywords attached to a crawled article."""
    seen: set[str] = set()
    out: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        key = normalize_text(text)
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    add(item.get("search_keyword"))
    for keyword in item.get("matched_search_keywords") or ():
        add(keyword)
    for ctx in item.get("matched_jw_search_contexts") or ():
        if not isinstance(ctx, dict):
            continue
        for keyword in ctx.get("matched_keywords") or ():
            add(keyword)
    return tuple(out)


def candidate_brands_for_item(item: dict[str, Any], brands: list[Tier2Brand]) -> list[Tier2Brand]:
    """Find Tier2 candidate brands from search provenance plus full-text exact hits.

    Search provenance is preserved as a candidate even when the current article
    body does not repeat the searched brand name. Full-text exact hits add
    additional brands so multi-brand Tier2 articles can later be promoted by
    the LLM tagger instead of being capped at the single search keyword.
    """
    title = str(item.get("title") or "")
    content = str(item.get("content") or item.get("article_text") or "")
    keywords = item_search_keywords(item)
    candidate_keys: set[str] = set()
    candidates: list[Tier2Brand] = []
    for brand in brands:
        brand_name = brand.brand_name.strip()
        if not brand_name:
            continue
        keyword_hit = any(normalize_text(keyword) == normalize_text(brand_name) for keyword in keywords)
        text_hit = has_exact_phrase(title, brand_name) or has_exact_phrase(content, brand_name)
        if not (keyword_hit or text_hit):
            continue
        if is_ambiguous_brand_name(brand_name) and not has_pharma_context(title, content):
            continue
        if brand.brand_key in candidate_keys:
            continue
        candidate_keys.add(brand.brand_key)
        candidates.append(brand)
    return candidates


def build_tier2_matches(item: dict[str, Any], brands: list[Tier2Brand]) -> list[dict[str, Any]]:
    title = str(item.get("title") or "")
    content = str(item.get("content") or item.get("article_text") or "")
    search_keywords = item_search_keywords(item)
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for brand in brands:
        match = score_exact_match(
            brand,
            title=title,
            content=content,
            search_keywords=search_keywords,
        )
        if match is None or match.brand_key in seen:
            continue
        seen.add(match.brand_key)
        matches.append(match.as_score_match())
    return matches
