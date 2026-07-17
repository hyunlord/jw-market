from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import filter_options
from pipeline.scripts.api.main import app


def test_build_filter_option_payload_includes_iqvia_molecule_desc_dimension() -> None:
    payload = filter_options.build_filter_option_payload(
        view="general",
        source="iqvia_nsa",
        market_id=None,
        dimensions=(
            filter_options.DimensionOptionRow("mfr", "제조사A", "제조사a", 3),
            filter_options.DimensionOptionRow("molecule_type", "SINGLE", "single", 2),
            filter_options.DimensionOptionRow("molecule_desc", "CARTEOLOL", "carteolol", 2),
            filter_options.DimensionOptionRow("strength", "5MG", "5mg", 1),
            filter_options.DimensionOptionRow("nhi", "NHI", "nhi", 1),
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


def test_build_filter_option_payload_labels_ubist_molecule_as_ingredient() -> None:
    payload = filter_options.build_filter_option_payload(
        view="general",
        source="ubist",
        market_id=None,
        dimensions=(
            filter_options.DimensionOptionRow(
                "molecule",
                "metformin / sitagliptin",
                "metformin / sitagliptin",
                4,
            ),
        ),
        atc_rows=({"atc4_code": "A10N1"},),
    )

    assert payload["dimensions"] == [
        {
            "dimension_type": "molecule",
            "label": "성분",
            "values": [
                {
                    "key": "metformin / sitagliptin",
                    "value": "metformin / sitagliptin",
                    "row_count": 4,
                }
            ],
        }
    ]


def test_filter_options_openapi_documents_key_value_request_contract() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    payload = str(schema["paths"]["/api/dynamic-market/filter-options"]["get"]["responses"]["200"])

    assert "일반 차원 요청에는 value를 다시 넣습니다" in payload
    assert "key='carteolol', value='CARTEOLOL'" in payload
    assert "pairs는 key='종별|진료과'" in payload
    operation = schema["paths"]["/api/dynamic-market/filter-options"]["get"]
    assert "전체 ATC4 universe" in operation["description"]
    brand_parameter = next(parameter for parameter in operation["parameters"] if parameter["name"] == "brand")
    assert "전체 목록을 제한하지 않고" in brand_parameter["description"]


def test_parse_atc_code_handles_deployed_source_shapes() -> None:
    assert filter_options.parse_atc_code("C07A0") == {"atc1": "C", "atc2": "C07", "atc3": "C07A", "atc4": "C07A0"}
    assert filter_options.parse_atc_code("C7A") == {"atc1": "C", "atc2": "C07", "atc3": "C07A", "atc4": "C7A"}
    assert filter_options.parse_atc_code("A10H") == {"atc1": "A", "atc2": "A10", "atc3": "A10H", "atc4": "A10H"}
    assert filter_options.parse_atc_code("A1A2") == {"atc1": "A", "atc2": "A01", "atc3": "A01A", "atc4": "A1A2"}
    assert filter_options.parse_atc_code("A11F") == {"atc1": "A", "atc2": "A11", "atc3": "A11F", "atc4": "A11F"}


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


def test_filter_options_keeps_general_market_unscoped_for_brand(monkeypatch) -> None:
    def fail_fetch_all(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("general brand membership must be resolved separately")

    monkeypatch.setattr(filter_options.db, "fetch_all", fail_fetch_all)

    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
        market_id=None,
    )

    assert resolved is None


def test_general_brand_atc4_uses_indexed_exact_lookup_before_compact_fallback(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        return [{"atc4_code": "C10A1"}]

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    result = filter_options._general_atc4_codes_for_brand(
        mart_db="jw_mart",
        source="ubist",
        brand="리바로",
    )

    assert result == ("C10A1",)
    assert len(calls) == 1
    assert "LOWER(REPLACE" not in calls[0][0]
    assert calls[0][1] == ["ubist", "리바로", "ubist", "리바로"]


def test_general_brand_atc4_falls_back_to_compact_lookup_only_after_exact_miss(monkeypatch) -> None:
    calls: list[tuple[str, list[object]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if len(calls) == 1:
            return []
        return [{"atc4_code": "C10A1"}, {"atc4_code": "C10C0"}]

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    result = filter_options._general_atc4_codes_for_brand(
        mart_db="jw_mart",
        source="ubist",
        brand="리 바 로",
    )

    assert result == ("C10A1", "C10C0")
    assert len(calls) == 2
    assert "LOWER(REPLACE" not in calls[0][0]
    assert "LOWER(REPLACE" in calls[1][0]


def test_general_filter_options_keeps_full_atc_universe_and_flags_all_brand_markets(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    all_atc4 = ("J01G1", "J01G2", "S01A0", "S02A0")
    brand_atc4 = ("J01G1", "J01G2", "S01A0")
    uncached_calls: list[tuple[str | None, tuple[str, ...]]] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        assert "mart_general_brand_metric" in sql
        assert params == ["iqvia_nsa", "에펙신", "iqvia_nsa", "에펙신"]
        return [{"atc4_code": code} for code in brand_atc4]

    def fake_uncached(**kwargs: object) -> dict[str, object]:
        market_id = kwargs["market_id"]
        atc4_codes = tuple(kwargs["atc4_codes"])
        uncached_calls.append((market_id, atc4_codes))
        return filter_options.build_filter_option_payload(
            view="general",
            source="iqvia_nsa",
            market_id=market_id,
            dimensions=(),
            atc_rows=tuple({"atc4_code": code} for code in all_atc4),
        )

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(filter_options, "_build_filter_options_uncached", fake_uncached)
    monkeypatch.setattr(filter_options, "_load_brand_dimension_matches", lambda **_kwargs: {})

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        view="general",
        source="iqvia_nsa",
        brand="에펙신",
    )

    assert uncached_calls == [(None, ())]
    assert [item["key"] for item in payload["atc"]["atc4"]] == list(all_atc4)
    assert [item["key"] for item in payload["atc"]["atc4"] if item["flag"]] == list(brand_atc4)
    assert payload["brand_matched"]["atc4"] == list(brand_atc4)


def test_general_filter_options_exposes_brand_flag_only_on_atc4(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()

    monkeypatch.setattr(
        filter_options.db,
        "fetch_all",
        lambda _sql, _params: [{"atc4_code": "C10A1"}],
    )
    monkeypatch.setattr(
        filter_options,
        "_build_filter_options_uncached",
        lambda **kwargs: filter_options.build_filter_option_payload(
            view="general",
            source="ubist",
            market_id=kwargs["market_id"],
            dimensions=(),
            atc_rows=({"atc4_code": "C10A1"}, {"atc4_code": "C10C0"}),
        ),
    )
    monkeypatch.setattr(filter_options, "_load_brand_dimension_matches", lambda **_kwargs: {})

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        view="general",
        source="ubist",
        brand="리바로",
    )

    for level in ("atc1", "atc2", "atc3"):
        assert all("flag" not in option for option in payload["atc"][level])
    assert [option["key"] for option in payload["atc"]["atc4"] if option["flag"]] == ["C10A1"]
    assert [option["key"] for option in payload["atc"]["atc4"] if not option["flag"]] == ["C10C0"]


def test_general_filter_options_unknown_brand_keeps_full_universe_without_flags(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    all_atc4 = ("A01A1", "C10A1")

    monkeypatch.setattr(filter_options.db, "fetch_all", lambda _sql, _params: [])
    monkeypatch.setattr(
        filter_options,
        "_build_filter_options_uncached",
        lambda **kwargs: filter_options.build_filter_option_payload(
            view="general",
            source="ubist",
            market_id=kwargs["market_id"],
            dimensions=(),
            atc_rows=tuple({"atc4_code": code} for code in all_atc4),
        ),
    )
    monkeypatch.setattr(filter_options, "_load_brand_dimension_matches", lambda **_kwargs: {})

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        view="general",
        source="ubist",
        brand="존재하지않는브랜드",
    )

    assert [item["key"] for item in payload["atc"]["atc4"]] == list(all_atc4)
    assert not any(item["flag"] for item in payload["atc"]["atc4"])
    assert payload["brand_matched"] == {"atc4": []}


def test_general_filter_options_treats_empty_atc4_codes_as_unscoped(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    captured: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        filter_options,
        "_build_filter_options_uncached",
        lambda **kwargs: captured.append(tuple(kwargs["atc4_codes"]))
        or filter_options.build_filter_option_payload(
            view="general",
            source="ubist",
            market_id=kwargs["market_id"],
            dimensions=(),
            atc_rows=({"atc4_code": "A01A1"}, {"atc4_code": "C10A1"}),
        ),
    )

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        general_dimension_db="jw_mart",
        view="general",
        source="ubist",
        atc4_codes=[],
    )

    assert captured == [()]
    assert [option["key"] for option in payload["atc"]["atc4"]] == ["A01A1", "C10A1"]


def test_build_filter_options_uses_resolved_market_id_for_payload_and_brand_match(monkeypatch) -> None:
    filter_options.clear_filter_option_cache()
    captured: dict[str, Any] = {}

    def fake_resolve(**_kwargs: object) -> None:
        return None

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
    monkeypatch.setattr(filter_options, "_general_atc4_codes_for_brand", lambda **_kwargs: ("C10A1",))
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

    assert payload["market_id"] is None
    assert payload["brand"] == "리바로"
    assert payload["brand_matched"] == {"seller": ["jw중외제약"], "atc4": ["C10A1"]}
    assert captured == {
        "uncached_market_id": None,
        "brand_match_market_id": None,
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


def test_general_filter_options_keeps_brand_universe_full_with_explicit_atc4_selection(monkeypatch) -> None:
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
            assert "atc4_code IN" not in sql
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

    assert captured_dimension_params == ["ubist", "sales"]
    assert captured_match_params[-2:] == ["C10A1", "C10C0"]
    assert payload["default_selections"]["atc4"] == ["C10A1", "C10C0"]
    assert payload["brand_matched"]["atc4"] == ["C10A1", "C10C0"]


def test_general_brand_dimension_matches_use_explicit_sidecar_alias(monkeypatch) -> None:
    captured_params: list[object] = []

    def fake_fetch_all(sql: str, params: list[object]) -> list[dict[str, object]]:
        nonlocal captured_params
        captured_params = params
        assert "brand_key IN (%s)" in sql
        return [
            {"dimension_type": "seller", "dimension_value_norm": "JW중외제약"},
            {"dimension_type": "form", "dimension_value_norm": "주사제(IJ)"},
            {"dimension_type": "route", "dimension_value_norm": "주사"},
            {"dimension_type": "reimbursement", "dimension_value_norm": "급여"},
            {
                "dimension_type": "molecule_strength",
                "dimension_value_norm": "tocilizumab 162㎎/0.9㎖ [520433BIJ]",
            },
        ]

    monkeypatch.setattr(filter_options.db, "fetch_all", fake_fetch_all)

    matches = filter_options._load_brand_dimension_matches(
        dimension_db="jw_mart",
        brand="악템라",
        view="general",
        source="ubist",
        market_id="M1C",
        measure="sales",
    )

    assert "악템라피하" in captured_params
    assert matches == {
        "seller": ["JW중외제약"],
        "form": ["주사제(IJ)"],
        "route": ["주사"],
        "reimbursement": ["급여"],
        "molecule_strength": ["tocilizumab 162㎎/0.9㎖ [520433BIJ]"],
    }


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
    assert all(
        option["flag"] is False
        for level in ("atc1", "atc2", "atc3", "atc4")
        for option in payload["atc"][level]
    )
    assert payload["default_selections"] == {
        "atc1": ["C"],
        "atc2": ["C10"],
        "atc3": ["C10A"],
        "atc4": ["C10A1"],
    }
    assert payload["applied_selections"] == {}


def test_filter_options_openapi_documents_deprecated_market_id_override() -> None:
    schema = app.openapi()
    params = schema["paths"]["/api/dynamic-market/filter-options"]["get"]["parameters"]
    by_name = {param["name"]: param for param in params}

    assert {"view", "source", "brand", "market_id"}.issubset(by_name)
    assert by_name["market_id"]["deprecated"] is True
    assert "호환" in by_name["market_id"]["description"]


def test_strategic_cd_atc4_option_codes_orders_like_option_payload(monkeypatch) -> None:
    # Given: mart rows in insertion order that differs from option order.
    from pipeline.scripts.api.dynamic_market import filter_options

    filter_options.strategic_cd_atc4_option_codes.cache_clear()
    monkeypatch.setattr(
        filter_options.db,
        "fetch_all",
        lambda sql, params=None: [
            {"atc4_code": "A10N3"},
            {"atc4_code": "A10C5"},
            {"atc4_code": "A10N1"},
        ],
    )

    # When: the canonical codes are resolved without an explicit mart db
    # (exercising the default DB_NAME resolution import path too).
    try:
        codes = filter_options.strategic_cd_atc4_option_codes("cd_003")
    finally:
        filter_options.strategic_cd_atc4_option_codes.cache_clear()

    # Then: order matches the filter-options atc.atc4 payload emission.
    assert codes == ("A10C5", "A10N1", "A10N3")


def test_strategic_cd_atc4_option_codes_rejects_non_cd_ids() -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    filter_options.strategic_cd_atc4_option_codes.cache_clear()
    assert filter_options.strategic_cd_atc4_option_codes("ml_006") == ()
    assert filter_options.strategic_cd_atc4_option_codes("") == ()


def test_normalize_view_accepts_cd_and_ml_alias() -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    assert filter_options.normalize_view("strategic") == "strategic"
    assert filter_options.normalize_view("strategic_ml") == "strategic"
    assert filter_options.normalize_view("strategic_cd") == "strategic_cd"
    assert filter_options.normalize_view("general") == "general"
    try:
        filter_options.normalize_view("strategic_xx")
    except Exception as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("invalid view must be rejected")


def test_resolver_derives_cd_market_from_brand(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    captured = {}

    class _Selection:
        market_kind = "cd"
        market_id = "cd_005"

    def fake_focus(**kwargs):
        captured.update(kwargs)
        return _Selection()

    monkeypatch.setattr(filter_options, "resolve_strategic_market_for_focus", fake_focus)

    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="strategic",
        source="ubist",
        brand="시그마트",
        market_id=None,
        strategic_market_kind="cd",
        measure="sales",
    )

    assert resolved == "cd_005"
    assert captured["view_kind"] == "strategic_cd"
    assert captured["focus_brand_key"] == "시그마트"


def test_strategic_view_without_kind_keeps_ml_resolution(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    class _Display:
        ml_id = "ml_005"

    monkeypatch.setattr(filter_options, "get_display_brand", lambda brand: _Display())
    monkeypatch.setattr(
        filter_options,
        "resolve_strategic_market_for_focus",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("cd resolver must not run for ML view")),
    )

    resolved = filter_options.resolve_filter_option_market_id(
        mart_db="jw_mart",
        view="strategic",
        source="ubist",
        brand="시그마트",
        market_id=None,
    )

    assert resolved == "ml_005"


def test_build_filter_options_routes_strategic_cd_scope(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    class _Selection:
        market_kind = "cd"
        market_id = "cd_005"

    monkeypatch.setattr(filter_options, "resolve_strategic_market_for_focus", lambda **kwargs: _Selection())

    seen = {}

    def fake_uncached(**kwargs):
        seen.update(kwargs)
        return {"atc": {"atc4": []}, "view": kwargs["view"], "market_id": kwargs["market_id"]}

    monkeypatch.setattr(filter_options, "_build_filter_options_uncached", fake_uncached)
    monkeypatch.setattr(filter_options, "_apply_option_state", lambda **kwargs: None)

    payload = filter_options.build_filter_options(
        mart_db="jw_mart",
        view="strategic_cd",
        source="ubist",
        brand="시그마트",
    )

    # CD scope flows through the resolved cd market id while downstream
    # builders keep the plain strategic view (existing behavior preserved).
    assert seen["market_id"] == "cd_005"
    assert seen["view"] == "strategic"
    assert payload["market_id"] == "cd_005"


def test_strategic_atc_rows_respect_requested_codes(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    universe = [
        {"atc4_code": "C1D"},
        {"atc4_code": "C1E"},
        {"atc4_code": "C7A"},
        {"atc4_code": "C8A"},
    ]
    monkeypatch.setattr(filter_options.db, "fetch_all", lambda sql, params=None: list(universe))

    # CD tab shape: the portal sends the cause-derived market codes.
    rows = filter_options._load_atc_rows(
        mart_db="jw_mart", view="strategic", source="ubist", market_id="ml_005",
        atc4_codes=("C1D",),
    )
    assert [r["atc4_code"] for r in rows] == ["C1D"]

    # ML tab shape: requested list equals the universe, nothing changes.
    rows = filter_options._load_atc_rows(
        mart_db="jw_mart", view="strategic", source="ubist", market_id="ml_005",
        atc4_codes=("C1D", "C1E", "C7A", "C8A"),
    )
    assert [r["atc4_code"] for r in rows] == ["C1D", "C1E", "C7A", "C8A"]


def test_strategic_atc_rows_keep_universe_without_codes(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    universe = [{"atc4_code": "C1D"}, {"atc4_code": "C1E"}]
    monkeypatch.setattr(filter_options.db, "fetch_all", lambda sql, params=None: list(universe))

    rows = filter_options._load_atc_rows(
        mart_db="jw_mart", view="strategic", source="ubist", market_id="ml_005",
        atc4_codes=(),
    )
    assert [r["atc4_code"] for r in rows] == ["C1D", "C1E"]


def test_strategic_atc_rows_fall_back_on_label_only_codes(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import filter_options

    universe = [{"atc4_code": "V6D"}, {"atc4_code": "V06D0"}]
    monkeypatch.setattr(filter_options.db, "fetch_all", lambda sql, params=None: list(universe))

    # Legacy frontend caches can still send display labels as atc4_codes; an
    # empty intersection must not blank the dropdown.
    rows = filter_options._load_atc_rows(
        mart_db="jw_mart", view="strategic", source="ubist", market_id="ml_015",
        atc4_codes=("악템라",),
    )
    assert [r["atc4_code"] for r in rows] == ["V6D", "V06D0"]

    # Mixed valid/invalid keeps the valid intersection only.
    rows = filter_options._load_atc_rows(
        mart_db="jw_mart", view="strategic", source="ubist", market_id="ml_015",
        atc4_codes=("악템라", "v6d"),
    )
    assert [r["atc4_code"] for r in rows] == ["V6D"]
