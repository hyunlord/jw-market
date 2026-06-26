from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Mapping

from .news_filter_extraction import extract_news_filter_entries, relevance_filter_entries, relevance_question_text
from .news_filter_sources import normalise_news_source
from .news_text import TextSearchSpec, parse_text_search


FilterValue = str | int | float | bool
FilterEntry = tuple[str, FilterValue]

_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "source",
        "date_from",
        "date_to",
        "recent_days",
        "category",
        "min_impact_score",
        "limit",
        "title_contains",
        "content_contains",
        "text_contains",
        "relevance_brands",
        "relevance_operator",
    }
)
_UNSUPPORTED_REASONS: Final[dict[str, str]] = {
}


@dataclass(frozen=True, slots=True)
class UnsupportedFilter:
    field: str
    value: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "value": self.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class NewsFilterPlan:
    source: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    recent_days: int | None = None
    category: str | None = None
    min_impact_score: float | None = None
    limit: int | None = None
    title_text: TextSearchSpec | None = None
    content_text: TextSearchSpec | None = None
    any_text: TextSearchSpec | None = None
    relevance_brands: tuple[str, ...] = ()
    relevance_operator: str = "OR"
    unsupported: tuple[UnsupportedFilter, ...] = ()

    def applied_filters(self, latest_event_date: str = "") -> dict[str, FilterValue]:
        filters: dict[str, FilterValue] = {}
        if self.source is not None:
            filters["source"] = self.source
        if self.date_from is not None:
            filters["date_from"] = self.date_from
        if self.date_to is not None:
            filters["date_to"] = self.date_to
        if self.recent_days is not None:
            filters["recent_days"] = self.recent_days
        if self.category is not None:
            filters["category"] = self.category
        if self.min_impact_score is not None:
            filters["min_impact_score"] = int(self.min_impact_score) if self.min_impact_score.is_integer() else self.min_impact_score
        if self.limit is not None:
            filters["limit"] = self.limit
        if self.relevance_brands:
            filters["relevance_brands"] = f" {self.relevance_operator} ".join(self.relevance_brands)
        if self.title_text is not None:
            filters["title_contains"] = self.title_text.label()
        if self.content_text is not None:
            filters["content_contains"] = self.content_text.label()
        if self.any_text is not None:
            filters["text_contains"] = self.any_text.label()
        if latest_event_date and self.recent_days is not None and self.date_from is None:
            derived = date.fromisoformat(latest_event_date[:10]).toordinal() - self.recent_days
            filters["date_from"] = date.fromordinal(derived).isoformat()
            filters["date_to"] = latest_event_date[:10]
        return filters

    @property
    def blocks_results(self) -> bool:
        return bool(self.unsupported)


def filter_entries_from_mapping(raw: Mapping[str, FilterValue] | None) -> tuple[FilterEntry, ...]:
    if raw is None:
        return ()
    entries = tuple((str(key), value) for key, value in raw.items() if isinstance(value, str | int | float | bool))
    return _dedupe_entries(entries)


def validate_news_filters(entries: tuple[FilterEntry, ...]) -> NewsFilterPlan:
    source: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    recent_days: int | None = None
    category: str | None = None
    min_impact_score: float | None = None
    limit: int | None = None
    title_text: TextSearchSpec | None = None
    content_text: TextSearchSpec | None = None
    any_text: TextSearchSpec | None = None
    relevance_brands: tuple[str, ...] = ()
    relevance_operator = "OR"
    unsupported: list[UnsupportedFilter] = []

    for field, value in entries:
        if field not in _ALLOWED_KEYS:
            unsupported.append(UnsupportedFilter(field, str(value), _UNSUPPORTED_REASONS.get(field, "지원하지 않는 뉴스 필터")))
            continue
        if field == "source":
            source = _normalise_source(str(value), unsupported)
        elif field == "date_from":
            date_from = _valid_date(str(value), field, unsupported)
        elif field == "date_to":
            date_to = _valid_date(str(value), field, unsupported)
        elif field == "recent_days":
            recent_days = _positive_int(value, field, unsupported)
        elif field == "category":
            category = str(value).strip() or None
        elif field == "min_impact_score":
            min_impact_score = _positive_float(value, field, unsupported)
        elif field == "limit":
            limit = _positive_int(value, field, unsupported)
        elif field == "title_contains":
            title_text = _text_spec(value, field, unsupported)
        elif field == "content_contains":
            content_text = _text_spec(value, field, unsupported)
        elif field == "text_contains":
            any_text = _text_spec(value, field, unsupported)
        elif field == "relevance_brands":
            relevance_brands = _brands(value)
        elif field == "relevance_operator":
            relevance_operator = "AND" if str(value).strip().upper() == "AND" else "OR"

    return NewsFilterPlan(
        source=source,
        date_from=date_from,
        date_to=date_to,
        recent_days=recent_days,
        category=category,
        min_impact_score=min_impact_score,
        limit=limit,
        title_text=title_text,
        content_text=content_text,
        any_text=any_text,
        relevance_brands=relevance_brands,
        relevance_operator=relevance_operator,
        unsupported=tuple(unsupported),
    )


def _text_spec(value: FilterValue, field: str, unsupported: list[UnsupportedFilter]) -> TextSearchSpec | None:
    spec = parse_text_search(str(value))
    if spec is None:
        unsupported.append(UnsupportedFilter(field, str(value), "빈 텍스트 검색어는 지원하지 않음"))
    return spec


def _brands(value: FilterValue) -> tuple[str, ...]:
    seen: set[str] = set()
    brands: list[str] = []
    for brand in str(value).split("|"):
        cleaned = brand.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        brands.append(cleaned)
    return tuple(brands)


def _normalise_source(value: str, unsupported: list[UnsupportedFilter]) -> str | None:
    source = value.strip()
    normalised = normalise_news_source(source)
    if normalised is None:
        unsupported.append(UnsupportedFilter("source", source, "지원하지 않는 뉴스 출처"))
        return None
    return normalised


def _valid_date(value: str, field: str, unsupported: list[UnsupportedFilter]) -> str | None:
    raw = value.strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        unsupported.append(UnsupportedFilter(field, raw, "YYYY-MM-DD 날짜만 지원"))
        return None


def _positive_int(value: FilterValue, field: str, unsupported: list[UnsupportedFilter]) -> int | None:
    number = int(value) if isinstance(value, int | float) or str(value).isdigit() else 0
    if number <= 0:
        unsupported.append(UnsupportedFilter(field, str(value), "양의 정수만 지원"))
        return None
    return number


def _positive_float(value: FilterValue, field: str, unsupported: list[UnsupportedFilter]) -> float | None:
    try:
        number = float(value)
    except ValueError:
        unsupported.append(UnsupportedFilter(field, str(value), "숫자만 지원"))
        return None
    if number < 0:
        unsupported.append(UnsupportedFilter(field, str(value), "0 이상의 숫자만 지원"))
        return None
    return number


def _dedupe_entries(entries: tuple[FilterEntry, ...]) -> tuple[FilterEntry, ...]:
    seen: set[str] = set()
    out: list[FilterEntry] = []
    for field, value in sorted(entries, key=lambda item: item[0]):
        if field in seen:
            continue
        seen.add(field)
        out.append((field, value))
    return tuple(out)
