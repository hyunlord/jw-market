from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContextError
from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import brands


DEFAULT_PAYLOAD = [
    {"brand": "리바로", "market_id": "strategy_001", "value": 1},
    {"brand": "가드메트", "market_id": "strategy_002", "value": 2},
]


def test_no_query_response_remains_byte_identical(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)

    response = TestClient(app).get("/api/brands")

    assert response.status_code == 200
    assert response.content == json.dumps(DEFAULT_PAYLOAD, ensure_ascii=False, separators=(",", ":")).encode()


def test_search_ranks_exact_prefix_then_substring_and_reports_more(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [
            {"brand_key": "마운자로정", "brand_name": "마운자로정", "market_size": 300},
            {"brand_key": "뉴마운자로", "brand_name": "뉴마운자로", "market_size": 900},
            {"brand_key": "마운자로", "brand_name": "마운자로", "market_size": 100},
        ],
    )
    monkeypatch.setattr(brands, "_contexts_for_brand", lambda _brand: [])

    response = TestClient(app).get("/api/brands?q=마운자로&limit=2")

    assert response.status_code == 200
    assert [item["brand"] for item in response.json()] == ["마운자로", "마운자로정"]
    assert response.headers["x-has-more"] == "true"
    assert response.headers["x-total-matches"] == "3"


def test_search_uses_shared_context_resolver_and_deduplicates_sources(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [{"brand_key": "마운자로", "brand_name": "마운자로", "market_size": 1}],
    )

    calls: list[str] = []

    def fake_resolve(*, brand: str, view_kind: str, market_id, source):
        calls.append(view_kind)
        raise DeepAnalysisContextError(
            status_code=409,
            error="ambiguous_source_context",
            message="choose",
            available_contexts=(
                {
                    "view_kind": view_kind,
                    "market_id": "A10B5" if view_kind == "general" else f"{view_kind[-2:]}_003",
                    "market_name": "당뇨병 치료제",
                    "source": "ubist",
                    "has_market_data": True,
                },
                {
                    "view_kind": view_kind,
                    "market_id": "A10B5" if view_kind == "general" else f"{view_kind[-2:]}_003",
                    "market_name": "당뇨병 치료제",
                    "source": "iqvia",
                    "has_market_data": True,
                },
            ),
        )

    monkeypatch.setattr(brands, "resolve_deep_analysis_context", fake_resolve)

    item = TestClient(app).get("/api/brands?q=마운자로").json()[0]

    assert calls == ["general", "strategic_ml", "strategic_cd"]
    assert len(item["contexts"]) == 3
    assert item["contexts"][0] == {
        "view_kind": "general",
        "market_id": "A10B5",
        "market_name": "당뇨병 치료제",
        "has_market_data": True,
    }


def test_search_keeps_known_brand_without_context(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [{"brand_key": "휴면브랜드", "brand_name": "휴면브랜드", "market_size": 0}],
    )
    monkeypatch.setattr(brands, "_contexts_for_brand", lambda _brand: [])

    item = TestClient(app).get("/api/brands?q=휴면").json()[0]

    assert item["contexts"] == []
    assert item["context_reason"] == "analysis_context_not_available"


def test_search_limit_is_capped_at_fifty(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_search_brand_candidates", lambda _query: [])
    response = TestClient(app).get("/api/brands?q=x&limit=51")
    assert response.status_code == 422


def test_query_alias_is_supported_and_conflicts_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda query: [{"brand_key": query, "brand_name": query, "market_size": 1}],
    )
    monkeypatch.setattr(brands, "_contexts_for_brand", lambda _brand: [])

    assert TestClient(app).get("/api/brands?query=마운자로").json()[0]["brand"] == "마운자로"
    assert TestClient(app).get("/api/brands?q=리바로&query=마운자로").status_code == 422
