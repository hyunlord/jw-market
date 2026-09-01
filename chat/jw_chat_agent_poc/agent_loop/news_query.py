from __future__ import annotations

import re


_NEWS_QUERY_STOPWORDS = frozenset(
    {
        "관련",
        "같이",
        "뉴스",
        "들",
        "봐줘",
        "보고",
        "보여줘",
        "소식",
        "이슈",
        "좀",
        "최근",
        "함께",
        "확인",
        "확인해줘",
    }
)
_NEWS_PRESENTATION_TERMS = frozenset(
    {
        "기사",
    }
)
_NEWS_REQUEST_TERMS = frozenset(
    {
        "알려줘",
        "알려주세요",
        "뭐",
        "뭐야",
        "무엇",
        "있나",
        "있나요",
        "있어",
        "있어요",
    }
)
_METRIC_INTENT_WORDS = frozenset(
    {
        "매출",
        "변동",
        "변화",
        "성장",
        "시장",
        "점유율",
        "증감",
        "추세",
        "추이",
        "환자",
        "환자수",
    }
)
_KOREAN_PARTICLES = ("이랑", "에서", "으로", "하고", "에게", "처럼", "관련", "랑", "은", "는", "을", "를", "이", "가", "와", "과", "도", "만")


def normalize_news_query(value: str, *, brand: str = "") -> str:
    normalized = re.sub(r"[^\w가-힣A-Za-z0-9.+-]+", " ", value).strip()
    if not normalized:
        return ""
    brand_terms = {brand.strip()} if brand.strip() else set()
    tokens: list[str] = []
    for raw_token in normalized.split():
        token = _strip_particle(raw_token)
        if not _is_meaningful_news_term(token, brand_terms):
            continue
        if token not in tokens:
            tokens.append(token)
    return " ".join(tokens[:3])


def _strip_particle(token: str) -> str:
    for particle in _KOREAN_PARTICLES:
        if token.endswith(particle) and len(token) > len(particle) + 1:
            return token[: -len(particle)]
    return token


def _is_meaningful_news_term(token: str, brand_terms: set[str]) -> bool:
    if not token or token in brand_terms:
        return False
    if (
        token in _NEWS_QUERY_STOPWORDS
        or token in _NEWS_PRESENTATION_TERMS
        or token in _NEWS_REQUEST_TERMS
        or token in _METRIC_INTENT_WORDS
    ):
        return False
    if token.isdigit():
        return False
    # Interrogative/request-only residue is not a corpus term. Brand tagging has
    # already narrowed the corpus, so an uncertain residue must not filter it.
    return re.search(r"(?:뭐|무엇|어때|알려(?:줘|주세요)?|있(?:나|나요|어|어요))$", token) is None
