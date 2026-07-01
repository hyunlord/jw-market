from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import filter_options
from pipeline.scripts.api.main import app


def test_filter_options_resolves_strategic_market_from_brand_catalog() -> None:
    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="strategic",
        source="ubist",
        brand="리바로",
        market_id=None,
    )

    assert resolved == "ml_006"


def test_filter_options_keeps_explicit_market_id_override(monkeypatch) -> None:
    def fail_fetch_all(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("explicit market_id must not resolve through DB")

    monkeypatch.setattr(filter_options.db, "fetch_all", fail_fetch_all)

    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
        market_id="c10a1",
    )

    assert resolved == "C10A1"


def test_filter_options_resolves_general_market_from_brand_mart(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        return [{"atc4_code": "C10A1"}]

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
        market_id=None,
    )

    assert resolved == "C10A1"
    assert calls == [
        (
            calls[0][0],
            ["ubist", "리바로", "리바로", "리바로", "리바로"],
        )
    ]
    assert "mart_general_brand_metric" in calls[0][0]


def test_build_filter_options_uses_resolved_market_id_for_payload_and_brand_match(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    captured: dict[str, Any] = {}

    def fake_resolve(**_kwargs: object) -> str:
        return "C10A1"

    def fake_uncached(**kwargs: object) -> dict[str, object]:
        captured["uncached_market_id"] = kwargs["market_id"]
        return {
            "view": kwargs["view"],
            "source": kwargs["source"],
            "market_id": kwargs["market_id"],
            "dimensions": [],
            "atc": {"selectable_levels": ["atc3", "atc4"]},
        }

    def fake_brand_matches(**kwargs: object) -> dict[str, list[str]]:
        captured["brand_match_market_id"] = kwargs["market_id"]
        return {"seller": ["jw중외제약"]}

    monkeypatch.setattr(filter_options, "resolve_filter_option_market_id", fake_resolve)
    monkeypatch.setattr(filter_options, "_build_filter_options_uncached", fake_uncached)
    monkeypatch.setattr(filter_options, "_load_brand_dimension_matches", fake_brand_matches)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
    )

    assert payload["market_id"] == "C10A1"
    assert payload["brand"] == "리바로"
    assert payload["brand_matched"] == {"seller": ["jw중외제약"]}
    assert captured == {
        "uncached_market_id": "C10A1",
        "brand_match_market_id": "C10A1",
    }


def test_filter_options_openapi_hides_market_id_override() -> None:
    schema = app.openapi()
    params = schema["paths"]["/api/dynamic-market/filter-options"]["get"]["parameters"]
    names = {param["name"] for param in params}

    assert {"view", "source", "brand"}.issubset(names)
    assert "market_id" not in names
