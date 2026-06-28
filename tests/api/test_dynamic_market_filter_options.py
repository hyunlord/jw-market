from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.filter_options import (
    DimensionOptionRow,
    build_brand_option_check,
    build_filter_option_payload,
)


def test_build_filter_option_payload_groups_dimensions_and_atc_levels() -> None:
    payload = build_filter_option_payload(
        view="general",
        source="ubist",
        market_id="A10",
        dimensions=(
            DimensionOptionRow("seller", "엘지화학", "엘지화학", 3),
            DimensionOptionRow("seller", "JW중외제약", "jw중외제약", 1),
            DimensionOptionRow("route", "경구", "경구", 2),
        ),
        atc_rows=(
            {"atc4_code": "A10N1", "atc4_desc": "GLP-1"},
            {"atc4_code": "A10S0", "atc4_desc": "SGLT2"},
        ),
    )

    assert payload["view"] == "general"
    assert [item["dimension_type"] for item in payload["dimensions"]] == [
        "seller",
        "molecule_strength",
        "form",
        "route",
        "reimbursement",
    ]
    assert payload["dimensions"][0]["values"][0]["value"] == "JW중외제약"
    assert payload["atc"]["atc1"][0]["value"] == "A"
    assert payload["atc"]["atc3"][0]["value"] == "A10N"
    assert payload["atc"]["selectable_levels"] == ["atc3", "atc4"]


def test_build_brand_option_check_returns_brand_matched_lists(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "idx_general_option_universe" in sql:
            return [
                {"dimension_type": "seller", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 10},
                {"dimension_type": "form", "dimension_value": "정제", "dimension_value_norm": "정제", "row_count": 5},
                {"dimension_type": "form", "dimension_value": "서방정", "dimension_value_norm": "서방정", "row_count": 2},
            ]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "C07A1", "atc4_desc": "beta"}]
        return [
            {"dimension_type": "seller", "dimension_value_norm": "태준제약"},
            {"dimension_type": "form", "dimension_value_norm": "서방정"},
            {"dimension_type": "form", "dimension_value_norm": "정제"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    payload = build_brand_option_check(
        mart_db="jw_mart",
        general_dimension_db="jw_dim",
        strategic_dimension_db="jw_strategic_dim",
        brand="미케란",
        view="general",
        source="ubist",
        market_id="C07",
    )

    assert payload["brand"] == "미케란"
    assert payload["brand_matched"] == {"seller": ["태준제약"], "form": ["서방정", "정제"]}
    assert payload["dimensions"][0]["dimension_type"] == "seller"
    assert any("jw_dim" in sql and "mart_general_filter_dimension_metric" in sql for sql, _ in calls)

    option_call = next(item for item in calls if "idx_general_option_universe" in item[0])
    brand_match_call = next(item for item in calls if "GROUP BY dimension_type, dimension_value_norm" in item[0])
    atc_call = next(item for item in calls if "mart_general_brand_metric" in item[0])
    assert option_call[1] == ["ubist"]
    assert "GROUP BY dimension_type, dimension_value_hash" in option_call[0]
    assert "atc4_code LIKE" not in option_call[0]
    assert brand_match_call[1][-1] == "C07%"
    assert "atc4_code LIKE" in brand_match_call[0]
    assert atc_call[1] == ["ubist"]
    assert "atc4_code LIKE" not in atc_call[0]


def test_build_brand_option_check_scopes_strategic_matches(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "GROUP BY dimension_type, dimension_value, dimension_value_norm" in sql:
            return [{"dimension_type": "mfr", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 2}]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"atc4_code": "C07A1", "atc4_desc": "beta"}]
        return [
            {"dimension_type": "mfr", "dimension_value_norm": "태준제약"},
            {"dimension_type": "nhi", "dimension_value_norm": "급여"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    payload = build_brand_option_check(
        mart_db="jw_mart",
        general_dimension_db="jw_dim",
        strategic_dimension_db="jw_strategic_dim",
        brand="미케란",
        view="strategic",
        source="iqvia",
        market_id="ml_005",
    )

    assert payload["source"] == "iqvia_nsa"
    assert payload["brand_matched"] == {"mfr": ["태준제약"], "nhi": ["급여"]}
    match_call = next((item for item in calls if "GROUP BY dimension_type, dimension_value_norm" in item[0]), None)
    assert match_call is not None
    assert "mart_strategic_filter_dimension_metric" in match_call[0]
    assert match_call[1][-2:] == ["ml", "ml_005"]
