"""Tokenization helpers tuned for short Korean/English pharma messages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import re
import unicodedata


TOKEN_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*|[가-힣]{2,}|[0-9]+(?:\.[0-9]+)?%?"
)
DOMAIN_UPPER = {
    "LDL-C",
    "P-CAB",
    "NODM",
    "TG",
    "DPP-4",
    "PPI",
    "PCAB",
    "GERD",
    "BPH",
    "CKD",
}
KOREAN_PARTICLE_SUFFIXES = ("으로", "에서", "에게", "보다", "까지", "부터", "와", "과", "은", "는", "이", "가", "을", "를", "의")
STOPWORDS = {
    "및",
    "으로",
    "에서",
    "보다",
    "관련",
    "대한",
    "효과",
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
}


def normalize_text(value: str) -> str:
    """Normalize message text without changing meaning or masking tokens."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def normalize_token(token: str) -> str:
    """Normalize one token while preserving domain abbreviations."""
    upper = token.upper()
    if upper in DOMAIN_UPPER:
        return upper
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", token):
        return upper if len(token) <= 4 else token.lower()
    if re.fullmatch(r"[가-힣]{3,}", token):
        for suffix in KOREAN_PARTICLE_SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                return token[: -len(suffix)]
    return token


def tokenize_message(message: str) -> list[str]:
    """Extract Korean terms and English/domain abbreviations from one message."""
    tokens = [normalize_token(match.group(0)) for match in TOKEN_PATTERN.finditer(normalize_text(message))]
    return [token for token in tokens if token not in STOPWORDS and len(token) > 1]


def token_counts(messages: Iterable[str]) -> Counter[str]:
    """Count tokens over an iterable of messages."""
    counts: Counter[str] = Counter()
    for message in messages:
        counts.update(tokenize_message(message))
    return counts


def ngram_counts(messages: Iterable[str], n: int) -> Counter[str]:
    """Count token n-grams over an iterable of messages."""
    counts: Counter[str] = Counter()
    for message in messages:
        tokens = tokenize_message(message)
        counts.update(" ".join(tokens[index : index + n]) for index in range(max(0, len(tokens) - n + 1)))
    return counts


def language_bucket(message: str) -> str:
    """Classify a short message into Korean, English, mixed, numeric, or empty."""
    text = normalize_text(message)
    if not text:
        return "empty"
    has_ko = bool(re.search(r"[가-힣]", text))
    has_en = bool(re.search(r"[A-Za-z]", text))
    if has_ko and has_en:
        return "mixed_ko_en"
    if has_ko:
        return "korean"
    if has_en:
        return "english"
    return "numeric_symbol"
