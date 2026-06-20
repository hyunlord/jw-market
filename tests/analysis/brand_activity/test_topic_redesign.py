"""Regression tests for the topic redesign analysis helpers."""

from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.topic_redesign.dictionary import assign_labels, build_label_candidates
from pipeline.scripts.analysis.brand_activity.topic_redesign.models import MessageRow
from pipeline.scripts.analysis.brand_activity.topic_redesign.text import contains_keyword, redact_snippet, tokenize


def test_tokenize_preserves_domain_abbreviations() -> None:
    """Tokenizer keeps pharma abbreviations that drive topic discovery."""
    assert {"P-CAB", "PPI", "LDL-C"} <= set(tokenize("P-CAB과 PPI 비교, LDL-C 강하"))


def test_a02b2_dictionary_is_multilabel() -> None:
    """A P-CAB message can match multiple provisional labels."""
    row = MessageRow("keyword", "keyword:1", "A02B2", "2026-04", "sample", "P-CAB은 PPI 대비 빠른 발현과 식사 무관 복용이 가능", "hash1")
    candidates = build_label_candidates("A02B2", [row], [])
    assigned = assign_labels([row], candidates)
    assert "P-CAB/PPI 비교" in assigned[row.row_id]
    assert "식사무관/빠른발현" in assigned[row.row_id]


def test_contains_keyword_compact_phrase() -> None:
    """Phrase matching tolerates common spacing differences in Korean copy."""
    assert contains_keyword("식사와 관계없이 복용", "관계 없")


def test_redact_snippet_masks_direct_identifiers() -> None:
    """Review snippets mask obvious URLs, emails, and phone-like identifiers."""
    snippet = redact_snippet("문의 test@example.com http://example.com 010-1234-5678")
    assert "[EMAIL]" in snippet
    assert "[URL]" in snippet
    assert "[PHONE]" in snippet
