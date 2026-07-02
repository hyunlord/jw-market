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
    assert payload["brand_matched"] == {"seller": ["jw중외제약"], "atc4": ["C10A1"]}
    assert captured == {
        "uncached_market_id": "C10A1",
        "brand_match_market_id": "C10A1",
    }


def test_general_filter_options_scope_dimensions_to_selected_atc4(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "mart_general_filter_dimension_metric" in sql:
            assert "atc4_code IN" in sql
            assert params == ["ubist", "sales", "C10A1", "C10C0"]
            return [
                {"dimension_type": "seller", "dimension_value": "JW중외제약", "dimension_value_norm": "JW중외제약", "row_count": 2},
                {"dimension_type": "molecule", "dimension_value": "PITAVASTATIN", "dimension_value_norm": "PITAVASTATIN", "row_count": 1},
            ]
        if "mart_general_brand_metric" in sql:
            assert "atc4_code IN" in sql
            return [{"atc4_code": "C10A1"}, {"atc4_code": "C10C0"}]
        raise AssertionError(sql)

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="general",
        source="ubist",
        market_id="C10A1,C10C0",
    )

    assert [dimension["dimension_type"] for dimension in payload["dimensions"]] == ["molecule", "seller"]
    assert payload["atc"]["atc4"] == [
        {"key": "C10A1", "value": "C10A1", "label": "C10A1", "level": "atc4", "parent": "C10A", "default": False, "selected": True, "flag": False},
        {"key": "C10C0", "value": "C10C0", "label": "C10C0", "level": "atc4", "parent": "C10C", "default": False, "selected": True, "flag": False},
    ]
    assert payload["applied_selections"]["atc4"] == ["C10A1", "C10C0"]


def test_strategic_filter_options_marks_all_values_default_and_flags_brand(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()

    def fake_resolve(**_kwargs: object) -> str:
        return "ml_006"

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "mart_strategic_filter_dimension_metric" in sql and "brand_name" not in sql:
            return [
                {"dimension_type": "seller", "dimension_value": "JW중외제약", "dimension_value_norm": "jw중외제약", "row_count": 3},
            ]
        if "mart_strategic_filter_dimension_metric" in sql and "brand_name" in sql:
            return [{"dimension_type": "seller", "dimension_value_norm": "jw중외제약"}]
        if "analysis_levels" in sql:
            return [{"analysis_levels": '{"class": {}, "molecule": {}}'}]
        if "JSON_EXTRACT(by_dimension" in sql:
            return [{"atc4_code": "C10A1"}]
        if "by_dimension" in sql and "brand_name" in sql:
            return [
                {"by_dimension": '{"class": "Statin", "molecule": "PITAVASTATIN"}'},
            ]
        if "by_dimension" in sql:
            return [
                {"by_dimension": '{"class": "Statin", "molecule": "PITAVASTATIN"}'},
                {"by_dimension": '{"class": "Statin", "molecule": "ROSUVASTATIN"}'},
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(filter_options, "resolve_filter_option_market_id", fake_resolve)
    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="strategic",
        source="ubist",
        brand="리바로",
    )

    class_dimension = next(dimension for dimension in payload["dimensions"] if dimension["dimension_type"] == "class")
    assert class_dimension["values"] == [
        {"key": "statin", "value": "Statin", "row_count": 2, "default": True, "selected": True, "flag": True},
    ]
    molecule = next(dimension for dimension in payload["dimensions"] if dimension["dimension_type"] == "molecule")
    assert molecule["values"] == [
        {"key": "pitavastatin", "value": "PITAVASTATIN", "row_count": 1, "default": True, "selected": True, "flag": True},
        {"key": "rosuvastatin", "value": "ROSUVASTATIN", "row_count": 1, "default": True, "selected": True, "flag": False},
    ]
    assert payload["default_selections"]["class"] == ["statin"]
    assert payload["default_selections"]["molecule"] == ["pitavastatin", "rosuvastatin"]
    assert payload["atc"]["atc4"][0]["default"] is True
    assert payload["atc"]["atc4"][0]["selected"] is True


def test_filter_options_openapi_hides_market_id_override() -> None:
    schema = app.openapi()
    params = schema["paths"]["/api/dynamic-market/filter-options"]["get"]["parameters"]
    names = {param["name"] for param in params}

    assert {"view", "source", "brand"}.issubset(names)
    assert "market_id" not in names
