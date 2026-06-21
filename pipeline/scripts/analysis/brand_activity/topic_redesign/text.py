"""Text normalization, token extraction, and privacy clipping helpers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import hashlib
import math
import re
from typing import Final
import unicodedata


TOKEN_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*|[가-힣]{2,}|[0-9]+(?:\.[0-9]+)?%?")
EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE: Final = re.compile(r"https?://\S+|www\.\S+")
PHONE_RE: Final = re.compile(r"\b0\d{1,2}-?\d{3,4}-?\d{4}\b")
DOMAIN_UPPER: Final = {
    "LDL-C",
    "P-CAB",
    "NODM",
    "TG",
    "DPP-4",
    "DPP4",
    "PPI",
    "PCAB",
    "GERD",
    "BPH",
    "CKD",
    "HB",
    "TIR",
    "TPN",
    "OSS",
    "JAK",
    "TNF",
    "IL-6",
    "PMS",
}
STOPWORDS: Final = {
    "및",
    "으로",
    "에서",
    "에게",
    "보다",
    "까지",
    "부터",
    "관련",
    "대한",
    "대해",
    "내용",
    "제품",
    "사용",
    "환자",
    "있는",
    "없는",
    "진료",
    "처방",
    "경우",
    "정보",
    "설명",
    "소개",
    "소개함",
    "부탁",
    "강조",
    "가능",
    "우수",
    "우수한",
    "가장",
    "다양한",
    "좋은",
    "MG",
    "ML",
    "10",
    "20",
    "100",
}
PARTICLE_SUFFIXES: Final = ("으로", "에서", "에게", "보다", "까지", "부터", "와", "과", "은", "는", "이", "가", "을", "를", "의", "에")


def normalize_text(value: str) -> str:
    """Normalize whitespace and Unicode composition without changing meaning."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def normalize_token(token: str) -> str:
    """Normalize a token while preserving pharma abbreviations and Korean stems."""
    upper = token.upper()
    if upper in DOMAIN_UPPER:
        return upper
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", token):
        return upper if len(token) <= 4 else token.lower()
    if re.fullmatch(r"[가-힣]{3,}", token):
        for suffix in PARTICLE_SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                return token[: -len(suffix)]
    return token


def tokenize(message: str) -> list[str]:
    """Return analysis tokens for one short Keyword/Meeting message."""
    raw_tokens = (normalize_token(match.group(0)) for match in TOKEN_RE.finditer(normalize_text(message)))
    return [token for token in raw_tokens if token not in STOPWORDS and len(token) > 1]


def token_counts(messages: Iterable[str]) -> Counter[str]:
    """Count normalized tokens over messages."""
    counts: Counter[str] = Counter()
    for message in messages:
        counts.update(tokenize(message))
    return counts


def ngram_counts(messages: Iterable[str], n: int) -> Counter[str]:
    """Count normalized token n-grams over messages."""
    counts: Counter[str] = Counter()
    for message in messages:
        parts = tokenize(message)
        counts.update(" ".join(parts[index : index + n]) for index in range(max(0, len(parts) - n + 1)))
    return counts


def text_sha256(value: str) -> str:
    """Hash sensitive source text for audit joins without raw text dumps."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def contains_keyword(message: str, keyword: str) -> bool:
    """Return true when a keyword or phrase occurs in a message."""
    haystack = normalize_text(message).upper()
    needle = normalize_text(keyword).upper()
    if not needle:
        return False
    # Pharma copy often omits spaces, so keep a compact fallback after boundary search.
    compact_hit = needle.replace(" ", "") in haystack.replace(" ", "")
    boundary_hit = bool(re.search(rf"(?<![A-Z0-9가-힣]){re.escape(needle)}(?![A-Z0-9가-힣])", haystack))
    return boundary_hit or compact_hit or needle in haystack


def matched_keywords(message: str, keywords: Iterable[str]) -> tuple[str, ...]:
    """Return unique dictionary keywords found in one message."""
    found: list[str] = []
    for keyword in keywords:
        if contains_keyword(message, keyword):
            found.append(keyword)
    return tuple(dict.fromkeys(found))


def redact_snippet(message: str, limit: int = 180) -> str:
    """Clip a representative review sentence and mask obvious direct identifiers."""
    text = URL_RE.sub("[URL]", EMAIL_RE.sub("[EMAIL]", PHONE_RE.sub("[PHONE]", normalize_text(message))))
    return text if len(text) <= limit else text[: limit - 1] + "..."


def is_noisy_candidate(term: str) -> bool:
    """Identify terms that are too generic or numeric to be good label candidates."""
    parts = tokenize(term)
    if not parts:
        return True
    numeric = sum(1 for part in parts if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%?", part))
    generic = sum(1 for part in parts if part in STOPWORDS or len(part) < 2)
    return numeric == len(parts) or (generic / len(parts)) > 0.5


def redundancy_rate(candidates: Iterable[str]) -> float:
    """Estimate duplicate semantic surface by token-set Jaccard overlap."""
    token_sets = [set(tokenize(candidate)) for candidate in candidates if tokenize(candidate)]
    if len(token_sets) < 2:
        return 0.0
    pairs = 0
    redundant = 0
    for left_index, left in enumerate(token_sets):
        for right in token_sets[left_index + 1 :]:
            pairs += 1
            union = len(left | right)
            overlap = len(left & right) / union if union else 0.0
            if overlap >= 0.65:
                redundant += 1
    return redundant / pairs if pairs else 0.0


def pmi_collocations(messages: Iterable[str], min_count: int) -> list[tuple[str, float, int]]:
    """Rank bigram collocations by PMI weighted with support."""
    token_counter = token_counts(messages)
    bigram_counter = ngram_counts(messages, 2)
    total_tokens = sum(token_counter.values()) or 1
    scored: list[tuple[str, float, int]] = []
    for bigram, count in bigram_counter.items():
        if count < min_count or is_noisy_candidate(bigram):
            continue
        left, right = bigram.split(" ", 1)
        denom = token_counter[left] * token_counter[right]
        pmi = math.log2((count * total_tokens) / denom) if denom else 0.0
        scored.append((bigram, pmi * math.log1p(count), count))
    return sorted(scored, key=lambda item: (-item[1], -item[2], item[0]))

