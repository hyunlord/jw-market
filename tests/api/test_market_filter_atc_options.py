from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

import pytest

from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError
from pipeline.scripts.api.market_filter_atc_options import build_market_filter_atc_options
from pipeline.scripts.api.main import app


def test_market_filter_atc_options_flags_brand_atc_in_strategic_view(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "mart_strategic_ml_brand_metric" in sql and "brand_key" not in sql:
            assert params == ["ubist", "ml_006"]
            return [{"atc4_code": "C10A1"}, {"atc4_code": "C10C"}]
        if "mart_strategic_ml_brand_metric" in sql and "brand_key" in sql:
            assert params == ["ubist", "리바로", "리바로", "리바로", "ml_006"]
            return [{"atc4_code": "C10A1"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.market_filter_atc_options.db.fetch_all", fake_fetch_all)

    payload = build_market_filter_atc_options(brand_name="리바로", view="strategic", source="ubist")

    assert payload["market_id"] == "ml_006"
    assert payload["flagged_atc4"] == ["C10A1"]
    assert payload["source"] == "ubist"
    assert payload["atc"]["atc1"] == [{"key": "C", "level": "atc1", "parent": None, "flag": True}]
    assert payload["atc"]["atc2"] == [{"key": "C10", "level": "atc2", "parent": "C", "flag": True}]
    assert payload["atc"]["atc3"] == [
        {"key": "C10A", "level": "atc3", "parent": "C10", "flag": True},
        {"key": "C10C", "level": "atc3", "parent": "C10", "flag": False},
    ]
    assert payload["atc"]["atc4"] == [
        {"key": "C10A1", "level": "atc4", "parent": "C10A", "flag": True},
        {"key": "C10C0", "level": "atc4", "parent": "C10C", "flag": False},
    ]


def test_market_filter_atc_options_flags_general_brand_atc_and_uses_source_universe(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "mart_general_brand_metric FORCE INDEX" in sql:
            assert params == ["iqvia_nsa", "가드렛", "가드렛", "가드렛"]
            return [{"atc4_code": "A10X9"}]
        if "FROM `jw_mart`.mart_general_brand_metric" in sql and "brand_key" in sql:
            assert params == ["iqvia_nsa", "가드렛", "가드렛", "가드렛"]
            return [{"atc4_code": "A10X9"}]
        if "FROM `jw_mart`.mart_general_brand_metric" in sql:
            assert params == ["iqvia_nsa"]
            return [{"atc4_code": "A10X9"}, {"atc4_code": "C10A1"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.market_filter_atc_options.db.fetch_all", fake_fetch_all)

    payload = build_market_filter_atc_options(brand_name="가드렛", view="general", source="iqvia")

    assert payload["source"] == "iqvia"
    assert payload["market_id"] == "A10X9"
    assert payload["flagged_atc4"] == ["A10X9"]
    assert payload["atc"]["atc4"][0]["flag"] is True
    assert payload["atc"]["atc4"][1]["flag"] is False
    assert any("SELECT DISTINCT atc4_code" in sql and "brand_key" not in sql for sql, _ in calls)


def test_market_filter_atc_options_is_get_only_and_exposed_in_openapi() -> None:
    schema = app.openapi()

    path = schema["paths"]["/api/market-filter/atc-options"]
    assert sorted(path) == ["get"]
    operation = path["get"]
    assert operation["summary"] == "시장필터 1단계 ATC 옵션"
    assert "MarketFilterAtcOptionsResponse" in str(operation)
    option_properties = schema["components"]["schemas"]["MarketFilterAtcOption"]["properties"]
    assert sorted(option_properties) == ["flag", "key", "level", "parent"]
    source_param = next(param for param in operation["parameters"] if param["name"] == "source")
    assert source_param["schema"]["enum"] == ["ubist", "iqvia"]

    client = TestClient(app)
    response = client.post("/api/market-filter/atc-options", json={"brand_name": "리바로", "view": "strategic", "source": "ubist"})
    assert response.status_code == 405


def test_market_filter_atc_options_rejects_internal_iqvia_source() -> None:
    with pytest.raises(DynamicMarketInputError, match="unsupported market filter source"):
        build_market_filter_atc_options(brand_name="가드렛", view="general", source="iqvia_nsa")


def test_market_filter_atc_options_canonicalizes_atc3_shaped_atc4_codes(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        if "mart_strategic_ml_brand_metric" in sql and "brand_key" not in sql:
            return [{"atc4_code": "C10C"}]
        if "mart_strategic_ml_brand_metric" in sql and "brand_key" in sql:
            return [{"atc4_code": "C10C"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.market_filter_atc_options.db.fetch_all", fake_fetch_all)

    payload = build_market_filter_atc_options(brand_name="리바로", view="strategic", source="ubist")

    assert payload["flagged_atc4"] == ["C10C0"]
    assert payload["atc"]["atc3"] == [{"key": "C10C", "level": "atc3", "parent": "C10", "flag": True}]
    assert payload["atc"]["atc4"] == [{"key": "C10C0", "level": "atc4", "parent": "C10C", "flag": True}]
