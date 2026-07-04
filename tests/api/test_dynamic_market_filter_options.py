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


def test_general_filter_options_splits_comma_atc4_codes_and_flags_all_defaults(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    captured_dimension_params: list[object] = []
    captured_match_params: list[object] = []

    def fake_resolve(**_kwargs: object) -> str | None:
        return None

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        nonlocal captured_dimension_params, captured_match_params
        if "mart_general_filter_dimension_metric" in sql and "brand_name" in sql:
            captured_match_params = params
            assert "atc4_code IN" in sql
            return [
                {"dimension_type": "seller", "dimension_value_norm": "jw중외제약"},
                {"dimension_type": "atc4", "dimension_value_norm": "C10A1"},
                {"dimension_type": "atc4", "dimension_value_norm": "C10C0"},
            ]
        if "mart_general_filter_dimension_metric" in sql:
            captured_dimension_params = params
            assert "atc4_code IN" in sql
            return []
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "C10A1"}, {"atc4_code": "C10C0"}]
        raise AssertionError(sql)

    monkeypatch.setattr(filter_options, "resolve_filter_option_market_id", fake_resolve)
    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
        atc4_codes=["C10A1,C10C0"],
    )

    assert captured_dimension_params == ["ubist", "sales", "C10A1", "C10C0"]
    assert captured_match_params[-2:] == ["C10A1", "C10C0"]
    assert payload["default_selections"]["atc4"] == ["C10A1", "C10C0"]
    assert payload["brand_matched"]["atc4"] == ["C10A1", "C10C0"]


def test_general_filter_options_adds_ubist_channel_axis_registry_from_raw_matrix(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "mart_general_filter_dimension_metric" in sql:
            return []
        if "SELECT atc4_code" in sql and "mart_general_brand_metric FORCE INDEX" in sql:
            return [{"atc4_code": "C10A1"}]
        if "channel_specialty_matrix" in sql:
            assert params == ["ubist", "sales", "C10A1"]
            return [
                {
                    "brand_key": "리바로",
                    "brand_name": "리바로",
                    "channel_specialty_matrix": '{"종합병원":{"순환기(Cardiology IM)":{"2026-05":10}},"의원":{"분리되지 않은 내과":{"2026-05":20}}}',
                },
                {
                    "brand_key": "경쟁",
                    "brand_name": "경쟁",
                    "channel_specialty_matrix": '{"종합병원":{"내분비(Endocrinology IM)":{"2026-05":30}}}',
                },
            ]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "C10A1"}]
        raise AssertionError(sql)

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
        market_id="C10A1",
    )

    assert payload["channel_axis"]["ubist"]["facility"] == [
        {"key": "의원", "value": "의원", "row_count": 1, "default": False, "selected": False, "flag": True},
        {"key": "종합병원", "value": "종합병원", "row_count": 2, "default": False, "selected": False, "flag": True},
    ]
    assert payload["channel_axis"]["ubist"]["specialty"][0] == {
        "key": "내분비(Endocrinology IM)",
        "value": "내분비(Endocrinology IM)",
        "row_count": 1,
        "default": False,
        "selected": False,
        "flag": False,
    }
    assert {
        "key": "종합병원|순환기(Cardiology IM)",
        "value": {"facility": "종합병원", "specialty": "순환기(Cardiology IM)"},
        "row_count": 1,
        "default": False,
        "selected": False,
        "flag": True,
    } in payload["channel_axis"]["ubist"]["pairs"]


def test_general_filter_options_adds_iqvia_audit_code_registry_from_matrix(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "mart_general_filter_dimension_metric" in sql:
            return []
        if "SELECT atc4_code" in sql and "mart_general_brand_metric FORCE INDEX" in sql:
            return [{"atc4_code": "C10A1"}]
        if "audit_code_matrix" in sql:
            assert params == ["iqvia_nsa", "sales", "C10A1"]
            return [
                {
                    "brand_key": "리바로",
                    "brand_name": "리바로",
                    "audit_code_matrix": '{"KPA":{"2025-Q4":100},"KHPA":{"2025-Q4":20}}',
                },
                {
                    "brand_key": "경쟁",
                    "brand_name": "경쟁",
                    "audit_code_matrix": '{"KPA":{"2025-Q4":50},"KCPA":{"2025-Q4":1}}',
                },
            ]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "C10A1"}]
        raise AssertionError(sql)

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        strategic_dimension_db="jw_mart",
        view="general",
        source="iqvia",
        brand="리바로",
        market_id="C10A1",
    )

    assert payload["channel_axis"]["iqvia"]["audit_code"] == [
        {"key": "KCPA", "value": "KCPA", "row_count": 1, "default": False, "selected": False, "flag": False},
        {"key": "KHPA", "value": "KHPA", "row_count": 1, "default": False, "selected": False, "flag": True},
        {"key": "KPA", "value": "KPA", "row_count": 2, "default": False, "selected": False, "flag": True},
    ]


def test_strategic_filter_options_exposes_only_atc_hierarchy(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()

    def fake_resolve(**_kwargs: object) -> str:
        return "ml_006"

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "JSON_EXTRACT(by_dimension" in sql:
            return [{"atc4_code": "C10A1"}]
        forbidden = (
            "mart_strategic_filter_dimension_metric",
            "analysis_levels",
            "by_dimension",
            "ubist_channel_by_code",
            "audit_code_matrix",
            "channel_specialty_matrix",
        )
        if any(token in sql for token in forbidden):
            raise AssertionError(f"strategic filter-options must stay ATC-only, got query: {sql}")
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

    assert payload["dimensions"] == []
    assert "channel_axis" not in payload
    assert payload["brand_matched"] == {}
    assert payload["atc"]["atc4"][0]["default"] is True
    assert payload["atc"]["atc4"][0]["selected"] is True
    assert payload["default_selections"] == {
        "atc1": ["C"],
        "atc2": ["C10"],
        "atc3": ["C10A"],
        "atc4": ["C10A1"],
    }
    assert payload["applied_selections"] == {}


def test_filter_options_openapi_hides_market_id_override() -> None:
    schema = app.openapi()
    params = schema["paths"]["/api/dynamic-market/filter-options"]["get"]["parameters"]
    names = {param["name"] for param in params}

    assert {"view", "source", "brand"}.issubset(names)
    assert "market_id" not in names
