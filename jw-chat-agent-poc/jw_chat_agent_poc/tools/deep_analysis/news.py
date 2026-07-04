from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Protocol

from jw_chat_agent_poc.agentic import FilterEntry, validate_news_filters
from jw_chat_agent_poc.agentic.news_filters import NewsFilterPlan
from jw_chat_agent_poc.agentic.news_text import TextSearchSpec

from .news_filtering import filter_events, select_events
from .news_corpus import CORPUS_EVENT_LIMIT, corpus_events_sql, events_from_corpus_rows
from .news_payload import DeepAnalysisNewsEvent, events_from_payload
from .news_relevance import EventMembership, event_key, membership_matches
from .news_transparency import no_data_message, no_data_summary, news_summary, transparency_fields


DEEP_ANALYSIS_EVENTS_SOURCE = "deep_analysis_events"


@dataclass(frozen=True, slots=True)
class DeepAnalysisNewsSnapshot:
    brand: str
    events: tuple[DeepAnalysisNewsEvent, ...]
    loaded_at: float


class DeepAnalysisNewsReader(Protocol):
    def load(self, brand: str) -> DeepAnalysisNewsSnapshot: ...


@dataclass(frozen=True, slots=True)
class StaticDeepAnalysisNewsReader:
    payloads_by_brand: dict[str, dict[str, Any]]

    def load(self, brand: str) -> DeepAnalysisNewsSnapshot:
        payload = self.payloads_by_brand.get(brand, {})
        return DeepAnalysisNewsSnapshot(
            brand=brand,
            events=tuple(events_from_payload(payload)),
            loaded_at=time.monotonic(),
        )


@dataclass(frozen=True, slots=True)
class FixtureDeepAnalysisNewsReader:
    fixture_path: Path = Path(__file__).resolve().parents[2] / "fixtures" / "deep_analysis_events.json"

    def load(self, brand: str) -> DeepAnalysisNewsSnapshot:
        if not self.fixture_path.exists():
            return DeepAnalysisNewsSnapshot(brand=brand, events=(), loaded_at=time.monotonic())
        payloads = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        if not isinstance(payloads, dict):
            raise TypeError("deep_analysis_events fixture must be a JSON object")
        payload = payloads.get(brand, {})
        return DeepAnalysisNewsSnapshot(
            brand=brand,
            events=tuple(events_from_payload(payload if isinstance(payload, dict) else {})),
            loaded_at=time.monotonic(),
        )


@dataclass(frozen=True, slots=True)
class MariaDbDeepAnalysisNewsReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_DEEP_NEWS_DB_READ_TIMEOUT_S", "10"))
    corpus_enabled: bool = os.environ.get("CHAT_DEEP_NEWS_CORPUS_ENABLED", "1").lower() not in {"0", "false", "no"}
    corpus_limit: int = int(os.environ.get("CHAT_DEEP_NEWS_CORPUS_LIMIT", str(CORPUS_EVENT_LIMIT)))

    def load(self, brand: str) -> DeepAnalysisNewsSnapshot:
        import pymysql

        with pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            connect_timeout=self.connect_timeout_s,
            read_timeout=self.read_timeout_s,
            write_timeout=self.read_timeout_s,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        ) as connection:
            if self.corpus_enabled:
                try:
                    corpus_events = self._load_corpus_events(connection, brand)
                except pymysql.MySQLError:
                    corpus_events = ()
                if corpus_events:
                    return DeepAnalysisNewsSnapshot(brand=brand, events=corpus_events, loaded_at=time.monotonic())
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT response_json "
                    "FROM cache_deep_analysis "
                    "WHERE brand=%s "
                    "ORDER BY updated_at DESC "
                    "LIMIT 1",
                    (brand,),
                )
                row = cursor.fetchone()

        if not row:
            return DeepAnalysisNewsSnapshot(brand=brand, events=(), loaded_at=time.monotonic())
        payload = json.loads(str(row["response_json"]))
        if not isinstance(payload, dict):
            raise TypeError("cache_deep_analysis.response_json must be a JSON object")
        return DeepAnalysisNewsSnapshot(brand=brand, events=tuple(events_from_payload(payload)), loaded_at=time.monotonic())

    def _load_corpus_events(self, connection: Any, brand: str) -> tuple[DeepAnalysisNewsEvent, ...]:
        with connection.cursor() as cursor:
            cursor.execute(corpus_events_sql(), (brand, brand, self.corpus_limit))
            rows = cursor.fetchall()
        if not isinstance(rows, list | tuple):
            return ()
        return events_from_corpus_rows(rows)


class TtlDeepAnalysisNewsCache:
    def __init__(self, reader: DeepAnalysisNewsReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._snapshots: dict[str, DeepAnalysisNewsSnapshot] = {}

    def snapshot(self, brand: str) -> DeepAnalysisNewsSnapshot:
        current = self._snapshots.get(brand)
        now = time.monotonic()
        if current is None or now - current.loaded_at > self._ttl_seconds:
            current = self._reader.load(brand)
            self._snapshots[brand] = current
        return current


class DeepAnalysisNewsTool:
    def __init__(
        self,
        mode: str | None = None,
        reader: DeepAnalysisNewsReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._mode = mode or os.environ.get("CHAT_DEEP_NEWS_MODE") or os.environ.get("CHAT_METRICS_MODE", "fixture")
        resolved_reader = reader or (MariaDbDeepAnalysisNewsReader() if self._mode == "cache" else FixtureDeepAnalysisNewsReader())
        ttl = ttl_seconds or int(os.environ.get("CHAT_DEEP_NEWS_TTL_SECONDS", "300"))
        self._cache = TtlDeepAnalysisNewsCache(resolved_reader, ttl_seconds=ttl)

    def related_news(self, brand: str, limit: int = 8, filter_entries: tuple[FilterEntry, ...] = ()) -> dict[str, Any]:
        plan = validate_news_filters(filter_entries)
        requested_brands = plan.relevance_brands or (brand,)
        snapshots = tuple(self._cache.snapshot(item) for item in requested_brands)
        snapshot_events = tuple(event for snapshot in snapshots for event in snapshot.events)
        latest = max((event.date for event in snapshot_events if event.date), default="")
        events = _events_matching_relevance(snapshots, requested_brands, plan.relevance_operator)
        filtered_events = () if plan.blocks_results else filter_events(events, plan, latest)
        effective_limit = plan.limit or limit
        selected = select_events(filtered_events, effective_limit, prioritize_impact=plan.min_impact_score is not None)
        transparency = transparency_fields(plan, latest)
        if not selected:
            return {
                "source": DEEP_ANALYSIS_EVENTS_SOURCE,
                "tool": "deep_analysis_related_news",
                "deterministic": True,
                **transparency,
                "data": {"items": [], "latest_event_date": latest},
                "summary_text": no_data_summary(brand, plan),
                "render_data": {
                    "brand": brand,
                    "status": "no_data",
                    "message": no_data_message(plan),
                    "items": [],
                    "latest_event_date": latest,
                    "selection": "on_list=true 우선, 없으면 impact_score 상위",
                    **transparency,
                    "deterministic": True,
                },
            }
        items = [_render_item(event, plan) for event in selected]
        return {
            "source": DEEP_ANALYSIS_EVENTS_SOURCE,
            "tool": "deep_analysis_related_news",
            "deterministic": True,
            **transparency,
            "data": {"items": items, "latest_event_date": latest},
            "summary_text": news_summary(brand, len(selected), plan),
            "render_data": {
                "brand": brand,
                "items": items,
                "latest_event_date": latest,
                "selection": "on_list=true 우선, 없으면 impact_score 상위",
                **transparency,
                "deterministic": True,
            },
        }


def _events_matching_relevance(
    snapshots: tuple[DeepAnalysisNewsSnapshot, ...],
    requested_brands: tuple[str, ...],
    operator: str,
) -> tuple[DeepAnalysisNewsEvent, ...]:
    events_by_key: dict[str, DeepAnalysisNewsEvent] = {}
    brands_by_key: dict[str, set[str]] = {}
    for snapshot in snapshots:
        for event in snapshot.events:
            key = event_key(event.url, event.title, event.date, event.source)
            events_by_key.setdefault(key, event)
            brands_by_key.setdefault(key, set()).add(snapshot.brand)
    selected: list[DeepAnalysisNewsEvent] = []
    for key, event in events_by_key.items():
        membership = EventMembership(event_key=key, brands=frozenset(brands_by_key.get(key, set())))
        if membership_matches(membership, requested_brands, operator):
            selected.append(event)
    return tuple(selected)


def _render_item(event: DeepAnalysisNewsEvent, plan: NewsFilterPlan) -> dict[str, Any]:
    item = event.to_render_item()
    excerpt = _match_excerpt(event, plan)
    if excerpt:
        item["match_excerpt"] = excerpt
    return item


def _match_excerpt(event: DeepAnalysisNewsEvent, plan: NewsFilterPlan) -> str:
    spec = plan.any_text or plan.content_text or plan.title_text
    if spec is None:
        return ""
    for text in (event.title, event.summary, event.body_full):
        excerpt = _excerpt_for_spec(text, spec)
        if excerpt:
            return excerpt
    return ""


def _excerpt_for_spec(text: str, spec: TextSearchSpec) -> str:
    if not text:
        return ""
    folded = text.casefold()
    for term in spec.terms:
        needle = term.casefold()
        index = folded.find(needle)
        if index < 0:
            continue
        start = max(0, index - 8)
        end = min(len(text), index + len(term) + 24)
        if start == 0 and end == len(text) and len(text) > len(term) + 12:
            end = min(len(text), index + len(term) + 12)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""
        return f"{prefix}{text[start:end].strip()}{suffix}"
    return ""
