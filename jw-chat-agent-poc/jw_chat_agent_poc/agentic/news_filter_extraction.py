from __future__ import annotations

import re

from .news_filter_sources import SOURCE_ALIASES


FilterValue = str | int | float | bool
FilterEntry = tuple[str, FilterValue]


def extract_news_filter_entries(question: str) -> tuple[FilterEntry, ...]:
    entries: list[FilterEntry] = []
    source = _source_from_question(question)
    if source:
        entries.append(("source", source))
    recent_days = _recent_days_from_question(question)
    if recent_days is not None:
        entries.append(("recent_days", recent_days))
    if any(token in question for token in ("중요", "영향도 높은", "핵심")):
        entries.append(("min_impact_score", 60))
    category = _category_from_question(question)
    if category is not None:
        entries.append(("category", category))
    entries.extend(_text_search_entries(question))
    return _dedupe_entries(tuple(entries))


def relevance_filter_entries(brands: tuple[str, ...], question: str) -> tuple[FilterEntry, ...]:
    if len(brands) < 2:
        return ()
    operator = "AND" if _is_relevance_and(question) else "OR"
    return (("relevance_brands", "|".join(brands)), ("relevance_operator", operator))


def relevance_question_text(question: str) -> str:
    text = question
    for label in ("제목", "타이틀", "내용", "본문", "요약"):
        text = re.sub(rf"{label}\s*에\s*.+?(?=$|뉴스|기사|보여|찾아)", "", text)
    return re.sub(r"뉴스\s*중\s*.+?\s*(?:들어간|포함|있는)(?:\s*거)?", "뉴스", text)


def _source_from_question(question: str) -> str | None:
    for alias in sorted(SOURCE_ALIASES, key=len, reverse=True):
        if alias in question:
            return alias
    for token in re.findall(r"[A-Za-z0-9가-힣]+신문", question):
        if token:
            return token
    return None


def _recent_days_from_question(question: str) -> int | None:
    if any(token in question for token in ("최근 한 달", "최근 1달", "최근 한달", "최근 30일")):
        return 30
    match = re.search(r"최근\s*(\d{1,3})\s*일", question)
    if match:
        return int(match.group(1))
    return None


def _category_from_question(question: str) -> str | None:
    for token in ("정책", "규제", "허가", "임상", "출시", "공급", "매출", "급여"):
        if token in question:
            return token
    return None


def _text_search_entries(question: str) -> tuple[FilterEntry, ...]:
    entries: list[FilterEntry] = []
    title_term = _contains_term(question, ("제목", "타이틀"))
    if title_term:
        entries.append(("title_contains", title_term))
    content_term = _contains_term(question, ("내용", "본문", "요약"))
    if content_term:
        entries.append(("content_contains", content_term))
    generic_term = _generic_text_term(question)
    if generic_term and not title_term and not content_term:
        entries.append(("text_contains", generic_term))
    return tuple(entries)


def _contains_term(question: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        match = re.search(rf"{label}\s*에\s*(.+?)(?:\s*(?:뉴스|기사|보여|찾아|$))", question)
        if match:
            return match.group(1).strip(" ?.")
    return None


def _generic_text_term(question: str) -> str | None:
    match = re.search(r"뉴스\s*중\s*(.+?)\s*(?:들어간|포함|있는)", question)
    if match:
        return match.group(1).strip(" ?.")
    return None


def _is_relevance_and(question: str) -> bool:
    return bool(re.search(r"둘\s*다|모두|\bAND\b", question, flags=re.IGNORECASE))


def _dedupe_entries(entries: tuple[FilterEntry, ...]) -> tuple[FilterEntry, ...]:
    seen: set[str] = set()
    out: list[FilterEntry] = []
    for field, value in sorted(entries, key=lambda item: item[0]):
        if field in seen:
            continue
        seen.add(field)
        out.append((field, value))
    return tuple(out)
