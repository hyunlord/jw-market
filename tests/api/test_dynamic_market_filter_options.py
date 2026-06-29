from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.filter_options import (
    DimensionOptionRow,
    build_brand_option_check,
    build_filter_options,
    build_filter_option_payload,
    clear_filter_option_cache,
    parse_atc_code,
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
            {"atc4_code": "A10N1"},
            {"atc4_code": "A10S0"},
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
    assert payload["atc"]["atc4"][0] == {"key": "A10N1", "value": "A10N1", "label": "A10N1"}
    assert payload["atc"]["selectable_levels"] == ["atc3", "atc4"]
    assert "atc4_desc" not in str(payload["atc"])


def test_build_filter_option_payload_includes_iqvia_molecule_desc_dimension() -> None:
    payload = build_filter_option_payload(
        view="general",
        source="iqvia_nsa",
        market_id=None,
        dimensions=(
            DimensionOptionRow("mfr", "제조사A", "제조사a", 3),
            DimensionOptionRow("molecule_type", "SINGLE", "single", 2),
            DimensionOptionRow("molecule_desc", "CARTEOLOL", "carteolol", 2),
            DimensionOptionRow("strength", "5MG", "5mg", 1),
            DimensionOptionRow("nhi", "NHI", "nhi", 1),
        ),
        atc_rows=({"atc4_code": "C07A0"},),
    )

    assert [item["dimension_type"] for item in payload["dimensions"]] == [
        "mfr",
        "molecule_type",
        "molecule_desc",
        "strength",
        "nhi",
    ]
    molecule_desc = payload["dimensions"][2]
    assert molecule_desc["label"] == "성분"
    assert molecule_desc["values"] == [{"key": "carteolol", "value": "CARTEOLOL", "row_count": 2}]


def test_parse_atc_code_handles_deployed_source_shapes() -> None:
    assert parse_atc_code("C07A0") == {"atc1": "C", "atc2": "C07", "atc3": "C07A", "atc4": "C07A0"}
    assert parse_atc_code("C7A") == {"atc1": "C", "atc2": "C07", "atc3": "C07A", "atc4": "C7A"}
    assert parse_atc_code("A10H") == {"atc1": "A", "atc2": "A10", "atc3": "A10H", "atc4": "A10H"}
    assert parse_atc_code("A1A2") == {"atc1": "A", "atc2": "A01", "atc3": "A01A", "atc4": "A1A2"}
    assert parse_atc_code("A11F") == {"atc1": "A", "atc2": "A11", "atc3": "A11F", "atc4": "A11F"}


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
            return [{"atc4_code": "C07A1"}]
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


def test_build_filter_options_reuses_cached_options_and_copies_payload(monkeypatch) -> None:
    clear_filter_option_cache()
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "idx_general_option_universe" in sql:
            return [{"dimension_type": "seller", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 10}]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "A1A2"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    first = build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")
    first["dimensions"][0]["values"].append({"key": "mutated", "value": "mutated", "row_count": 1})
    second = build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")

    assert len(calls) == 2
    assert second["dimensions"][0]["values"] == [{"key": "태준제약", "value": "태준제약", "row_count": 10}]
    assert second["atc"]["atc2"] == [{"key": "A01", "value": "A01", "label": "A01"}]

    clear_filter_option_cache()
    build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")

    assert len(calls) == 4
    clear_filter_option_cache()


def test_filter_option_cache_can_be_disabled(monkeypatch) -> None:
    clear_filter_option_cache()
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "idx_general_option_universe" in sql:
            return [{"dimension_type": "seller", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 10}]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "A1A2"}]
        raise AssertionError(sql)

    monkeypatch.setenv("DYNAMIC_MARKET_FILTER_OPTIONS_CACHE", "0")
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")
    build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")

    assert len(calls) == 4
    clear_filter_option_cache()


def test_build_brand_option_check_caches_options_but_not_brand_matches(monkeypatch) -> None:
    clear_filter_option_cache()
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append(sql)
        if "idx_general_option_universe" in sql:
            return [{"dimension_type": "seller", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 10}]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "A1A2"}]
        if "GROUP BY dimension_type, dimension_value_norm" in sql:
            return [{"dimension_type": "seller", "dimension_value_norm": f"brand-match-{len(calls)}"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    first = build_brand_option_check(mart_db="jw_mart", brand="미케란", view="general", source="ubist", market_id="A1")
    second = build_brand_option_check(mart_db="jw_mart", brand="미케란", view="general", source="ubist", market_id="A1")

    assert sum("idx_general_option_universe" in sql for sql in calls) == 1
    assert sum("mart_general_brand_metric" in sql for sql in calls) == 1
    assert sum("GROUP BY dimension_type, dimension_value_norm" in sql for sql in calls) == 2
    assert first["brand_matched"] != second["brand_matched"]
    assert first["dimensions"] == second["dimensions"]
    assert first["atc"] == second["atc"]
    clear_filter_option_cache()


def test_build_filter_options_accepts_optional_brand_without_changing_plain_contract(monkeypatch) -> None:
    clear_filter_option_cache()
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append(sql)
        if "idx_general_option_universe" in sql:
            return [{"dimension_type": "seller", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 10}]
        if "mart_general_brand_metric" in sql:
            return [{"atc4_code": "A1A2"}]
        if "GROUP BY dimension_type, dimension_value_norm" in sql:
            return [{"dimension_type": "seller", "dimension_value_norm": "태준제약"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.filter_options.db.fetch_all", fake_fetch_all)

    plain = build_filter_options(mart_db="jw_mart", view="general", source="ubist", market_id="A1")
    with_brand = build_filter_options(mart_db="jw_mart", brand="미케란", view="general", source="ubist", market_id="A1")

    assert "brand" not in plain
    assert "brand_matched" not in plain
    assert with_brand["brand"] == "미케란"
    assert with_brand["brand_matched"] == {"seller": ["태준제약"]}
    assert with_brand["dimensions"] == plain["dimensions"]
    assert with_brand["atc"] == plain["atc"]
    assert sum("idx_general_option_universe" in sql for sql in calls) == 1
    assert sum("mart_general_brand_metric" in sql for sql in calls) == 1
    assert sum("GROUP BY dimension_type, dimension_value_norm" in sql for sql in calls) == 1
    clear_filter_option_cache()


def test_build_brand_option_check_scopes_strategic_matches(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "GROUP BY dimension_type, dimension_value, dimension_value_norm" in sql:
            return [
                {"dimension_type": "mfr", "dimension_value": "태준제약", "dimension_value_norm": "태준제약", "row_count": 2},
                {"dimension_type": "molecule_desc", "dimension_value": "CARTEOLOL", "dimension_value_norm": "carteolol", "row_count": 1},
            ]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"atc4_code": "C07A1"}]
        return [
            {"dimension_type": "mfr", "dimension_value_norm": "태준제약"},
            {"dimension_type": "molecule_desc", "dimension_value_norm": "carteolol"},
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
    assert payload["brand_matched"] == {"mfr": ["태준제약"], "molecule_desc": ["carteolol"], "nhi": ["급여"]}
    match_call = next((item for item in calls if "GROUP BY dimension_type, dimension_value_norm" in item[0]), None)
    assert match_call is not None
    assert "mart_strategic_filter_dimension_metric" in match_call[0]
    assert match_call[1][-2:] == ["ml", "ml_005"]
