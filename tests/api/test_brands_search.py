from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContextError
from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import brands


DEFAULT_PAYLOAD = [
    {
        "brand": "리바로",
        "market_id": "strategy_001",
        "sources": ["UBIST", "IQVIA"],
        "strategic_sources": ["UBIST"],
        "general_sources": ["UBIST", "IQVIA"],
        "value": 1,
    },
    {
        "brand": "가드메트",
        "market_id": "strategy_002",
        "sources": ["UBIST", "IQVIA"],
        "strategic_sources": ["UBIST", "IQVIA"],
        "general_sources": ["UBIST", "IQVIA"],
        "value": 2,
    },
]


def test_no_query_response_remains_byte_identical(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)

    response = TestClient(app).get("/api/brands")

    assert response.status_code == 200
    assert response.content == json.dumps(DEFAULT_PAYLOAD, ensure_ascii=False, separators=(",", ":")).encode()


def test_exact_brand_query_returns_exact_match(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [{"brand_key": "리바로", "brand_name": "리바로", "market_size": 1}],
    )
    monkeypatch.setattr(
        brands,
        "_context_options_for_brand",
        lambda _brand: ([], ["UBIST"], ["UBIST"]),
    )

    response = TestClient(app).get("/api/brands?q=리바로")

    assert response.status_code == 200
    assert response.json() == [
        {
            "brand": "리바로",
            "sources": ["UBIST"],
            "strategic_sources": ["UBIST"],
            "general_sources": ["UBIST"],
            "contexts": [],
            "is_jw_target": True,
            "context_reason": "analysis_context_not_available",
        }
    ]


def test_search_uses_sargable_compact_exact_indexes_without_wildcards(monkeypatch) -> None:
    captured: list[tuple[str, tuple[str, ...]]] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        captured.append((sql, params))
        return [
            {
                "brand_key": "리바로",
                "brand_name": "리바로",
                "raw_value_history": '{"2026-04":1}',
                "source": "ubist",
            }
        ]

    monkeypatch.setattr(brands.db, "fetch_all", fake_fetch_all)

    assert len(brands._search_brand_candidates("  리 바 로  ")) == 1
    assert len(captured) == 1
    sql, params = captured[0]
    assert " LIKE " not in sql
    assert "REPLACE(" not in sql
    assert "brand_key = %s" in sql
    assert "brand_name = %s" in sql
    assert "UNION ALL" in sql
    assert params == ("리바로", "리바로", "리바로")


def test_search_exact_miss_never_falls_back_to_non_sargable_scan(monkeypatch) -> None:
    captured: list[str] = []

    def fake_fetch_all(sql: str, _params: tuple[str, ...]) -> list[dict]:
        captured.append(sql)
        return []

    monkeypatch.setattr(brands.db, "fetch_all", fake_fetch_all)

    assert brands._search_brand_candidates("없는 브랜드") == []
    assert len(captured) == 1
    assert all("REPLACE(" not in sql for sql in captured)


@pytest.mark.parametrize("query", ["리바", "바로"])
def test_partial_brand_query_returns_no_matches(monkeypatch, query: str) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(brands.db, "fetch_all", lambda _sql, _params: [])

    response = TestClient(app).get("/api/brands", params={"q": query})

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["x-has-more"] == "false"
    assert response.headers["x-total-matches"] == "0"


def test_search_orders_exact_matches_by_sales_and_preserves_contract(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [
            {"brand_key": "마운자로", "brand_name": "마운자로정", "market_size": 300},
            {"brand_key": "마운자로정", "brand_name": "마운자로", "market_size": 900},
        ],
    )
    monkeypatch.setattr(
        brands,
        "_context_options_for_brand",
        lambda _brand: (
            [{"view_kind": "general", "market_id": "A10S0"}],
            ["IQVIA"],
            [],
        ),
    )

    response = TestClient(app).get("/api/brands?q=마운자로&limit=1")

    assert response.status_code == 200
    assert response.json() == [
        {
            "brand": "마운자로",
            "sources": ["IQVIA"],
            "strategic_sources": [],
            "general_sources": ["IQVIA"],
            "contexts": [{"view_kind": "general", "market_id": "A10S0"}],
            "is_jw_target": False,
        }
    ]
    assert list(response.json()[0]) == [
        "brand",
        "sources",
        "strategic_sources",
        "general_sources",
        "contexts",
        "is_jw_target",
    ]
    assert response.headers["x-has-more"] == "true"
    assert response.headers["x-total-matches"] == "2"
    assert response.headers["x-result-limit"] == "1"


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
    assert item["sources"] == ["UBIST", "IQVIA"]
    assert item["general_sources"] == ["UBIST", "IQVIA"]
    assert item["strategic_sources"] == ["UBIST", "IQVIA"]


def test_search_sources_exclude_contexts_without_market_data(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [{"brand_key": "마운자로", "brand_name": "마운자로", "market_size": 1}],
    )

    def fake_resolve(*, brand: str, view_kind: str, market_id, source):
        raise DeepAnalysisContextError(
            status_code=409,
            error="ambiguous_source_context",
            message="choose",
            available_contexts=(
                {
                    "view_kind": view_kind,
                    "market_id": "A10S0",
                    "market_name": "GLP-1",
                    "source": "ubist",
                    "has_market_data": False,
                },
                {
                    "view_kind": view_kind,
                    "market_id": "A10S0",
                    "market_name": "GLP-1",
                    "source": "iqvia",
                    "has_market_data": True,
                },
            ),
        )

    monkeypatch.setattr(brands, "resolve_deep_analysis_context", fake_resolve)

    item = TestClient(app).get("/api/brands?q=마운자로").json()[0]

    assert item["sources"] == ["IQVIA"]
    assert item["general_sources"] == ["IQVIA"]
    assert item["strategic_sources"] == ["IQVIA"]


def test_search_keeps_known_brand_without_context(monkeypatch) -> None:
    monkeypatch.setattr(brands, "_default_brands", lambda: DEFAULT_PAYLOAD)
    monkeypatch.setattr(
        brands,
        "_search_brand_candidates",
        lambda _query: [{"brand_key": "휴면브랜드", "brand_name": "휴면브랜드", "market_size": 0}],
    )
    monkeypatch.setattr(brands, "_context_options_for_brand", lambda _brand: ([], [], []))

    item = TestClient(app).get("/api/brands?q=휴면브랜드").json()[0]

    assert item["contexts"] == []
    assert item["sources"] == []
    assert item["general_sources"] == []
    assert item["strategic_sources"] == []
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
    monkeypatch.setattr(brands, "_context_options_for_brand", lambda _brand: ([], [], []))

    assert TestClient(app).get("/api/brands?query=마운자로").json()[0]["brand"] == "마운자로"
    assert TestClient(app).get("/api/brands?q=리바로&query=마운자로").status_code == 422
