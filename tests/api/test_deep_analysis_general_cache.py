from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from pipeline.scripts.api.routes import deep_analysis


def _row(scope: str, atc4: str | None = None) -> dict[str, Any]:
    return {
        "response_json": json.dumps(
            {
                "brand": "멀티브랜드",
                "market_id": "ml_001" if scope == "strategic" else f"general:{atc4}",
                "data": {"forecast": {"by_combo": {}}, "scope": scope},
            },
            ensure_ascii=False,
        ),
        "brand_factors": json.dumps({"atc": [atc4] if atc4 else [], "ubist": {}, "iqvia": {}}, ensure_ascii=False),
        "updated_at": "2026-07-09T09:00:00+09:00",
        "atc4_code": atc4,
    }


def test_deep_analysis_prefers_existing_strategic_cache_when_atc4_is_absent(monkeypatch) -> None:
    # Given: both strategic and general cache rows exist for the brand.
    queries: list[str] = []

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        queries.append(sql)
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        if "cache_deep_analysis_general" in sql:
            return _row("general", "A10A0")
        return _row("strategic")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: legacy deep-analysis URL is requested without an ATC4 selector.
    payload = deep_analysis.deep_analysis("멀티브랜드")

    # Then: the strategic cache contract remains the default.
    assert payload["market_id"] == "ml_001"
    assert payload["data"]["scope"] == "strategic"
    assert not any("cache_deep_analysis_general" in query for query in queries)


def test_deep_analysis_uses_general_cache_for_explicit_atc4(monkeypatch) -> None:
    # Given: the caller selects a general-view ATC4 cache row.
    seen_params: list[list[str]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        seen_params.append(params)
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        assert "cache_deep_analysis_general" in sql
        assert "AND atc4_code = %s" in sql
        return _row("general", "A10N3")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: an ATC4 selector is supplied.
    payload = deep_analysis.deep_analysis("멀티브랜드", atc4="A10N3")

    # Then: the general row is served without touching the strategic cache.
    assert payload["market_id"] == "general:A10N3"
    assert payload["data"]["scope"] == "general"
    assert seen_params[0] == ["멀티브랜드", "멀티브랜드", "A10N3"]


def test_deep_analysis_generates_general_cache_for_explicit_atc4_miss(monkeypatch) -> None:
    # Given: the requested general-view cache row is absent but can be generated on demand.
    calls: list[tuple[str, str | None]] = []

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        if "cache_deep_analysis_general" in sql:
            return None
        raise AssertionError("explicit ATC4 requests should not query the strategic cache")

    def fake_generate(brand: str, atc4: str | None) -> dict[str, Any]:
        calls.append((brand, atc4))
        return _row("general", "A10N3")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis, "_build_general_deep_analysis_on_demand", fake_generate)

    # When: the caller requests a cache-miss general market.
    payload = deep_analysis.deep_analysis("멀티브랜드", atc4="A10N3")

    # Then: the API generates, serves, and later persists that general forecast row.
    assert payload["market_id"] == "general:A10N3"
    assert payload["data"]["scope"] == "general"
    assert calls == [("멀티브랜드", "A10N3")]


def test_deep_analysis_regenerates_expired_general_cache(monkeypatch) -> None:
    # Given: a general-view cache row exists but its TTL has expired.
    calls: list[tuple[str, str | None]] = []
    stale_row = {
        **_row("general", "A10N3"),
        "expires_at": datetime.now(deep_analysis.KST) - timedelta(days=1),
    }

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        if "cache_deep_analysis_general" in sql:
            return stale_row
        raise AssertionError("explicit ATC4 requests should not query the strategic cache")

    def fake_generate(brand: str, atc4: str | None) -> dict[str, Any]:
        calls.append((brand, atc4))
        return _row("general", "A10N3")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis, "_build_general_deep_analysis_on_demand", fake_generate)

    # When: the stale cache row is requested.
    payload = deep_analysis.deep_analysis("멀티브랜드", atc4="A10N3")

    # Then: the API rebuilds the row before responding.
    assert payload["market_id"] == "general:A10N3"
    assert calls == [("멀티브랜드", "A10N3")]


def test_deep_analysis_falls_back_to_first_general_atc4_when_strategic_absent(monkeypatch) -> None:
    # Given: no strategic cache exists, but a general cache row exists.
    queries: list[str] = []

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        queries.append(sql)
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        if "cache_deep_analysis_general" in sql:
            assert "ORDER BY atc4_code ASC" in sql
            return _row("general", "B01C0")
        return None

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: a brand outside the strategic cache is requested.
    payload = deep_analysis.deep_analysis("멀티브랜드")

    # Then: the deterministic general-view tie-break row is served.
    assert payload["market_id"] == "general:B01C0"
    assert payload["data"]["scope"] == "general"
    assert any("cache_deep_analysis_general" in query for query in queries)


def test_deep_analysis_generates_first_general_atc4_when_no_cache_exists(monkeypatch) -> None:
    # Given: neither strategic nor general cache currently has the brand.
    calls: list[tuple[str, str | None]] = []

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        return None

    def fake_generate(brand: str, atc4: str | None) -> dict[str, Any]:
        calls.append((brand, atc4))
        return _row("general", "B01C0")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis, "_build_general_deep_analysis_on_demand", fake_generate)

    # When: a general-only brand is requested without an ATC4 selector.
    payload = deep_analysis.deep_analysis("멀티브랜드")

    # Then: the deterministic first ATC4 market is generated and served.
    assert payload["market_id"] == "general:B01C0"
    assert payload["data"]["scope"] == "general"
    assert calls == [("멀티브랜드", None)]


def test_deep_analysis_reports_forecast_unavailable_for_uncalculable_general_brand(monkeypatch) -> None:
    # Given: no cache row exists and the general forecast builder cannot produce one.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql or "agent3_brand_strength" in sql:
            return None
        return None

    def fake_generate(brand: str, atc4: str | None) -> dict[str, Any]:
        raise deep_analysis.GeneralForecastUnavailable(
            brand=brand,
            atc4=atc4,
            reason="general_market_not_found",
        )

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis, "_build_general_deep_analysis_on_demand", fake_generate)

    # When/Then: the API distinguishes calculation failure from a legacy cache miss.
    try:
        deep_analysis.deep_analysis("미생성브랜드", atc4="Z99Z9")
    except deep_analysis.HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == {
            "error": "forecast_unavailable",
            "brand": "미생성브랜드",
            "atc4": "Z99Z9",
            "reason": "general_market_not_found",
        }
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected forecast_unavailable HTTPException")
