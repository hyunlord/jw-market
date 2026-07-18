from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from pipeline.scripts.api.brand_activity_brand_filters import applied_brand_filter
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandCandidate,
    BrandSetInputError,
    _brand_candidates,
    _resolve_strategic_brand_context,
    _select_choices,
    resolve_brand_set,
    validate_audit_code_axis,
)
from pipeline.scripts.api import brand_activity_brand_resolver as resolver
from pipeline.scripts.api.brand_activity_channel_axis import (
    audit_code_sales_value,
    parse_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContextError


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    (
        ("pipeline.scripts.api.brand_activity_topic_matrix", "get_topic_brand_payload"),
        ("pipeline.scripts.api.brand_activity_interest_rx_matrix", "get_interest_rx_matrix"),
        ("pipeline.scripts.api.brand_activity_csd_timeseries", "get_csd_timeseries"),
        ("pipeline.scripts.api.brand_activity_csd_activity_series", "get_csd_activity_series"),
    ),
)
def test_brand_activity_routes_enable_strategic_choice_prefilter(
    module_name: str,
    function_name: str,
) -> None:
    function = getattr(importlib.import_module(module_name), function_name)
    tree = ast.parse(inspect.getsource(function))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_brand_set"
    ]

    assert len(calls) == 1
    keyword = next(
        (item for item in calls[0].keywords if item.arg == "prefilter_strategic_choices"),
        None,
    )
    assert keyword is not None
    assert isinstance(keyword.value, ast.Constant)
    assert keyword.value.value is True


def test_brand_filter_uses_or_within_dimension_and_and_across_dimensions() -> None:
    candidates = (
        _candidate("선택", rank=99, sales=1.0, dimensions={"atc4": ("C10A1",), "molecule": ("other",), "class": ("Z",)}),
        _candidate("A", rank=1, sales=100.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
        _candidate("B", rank=2, sales=90.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("복합제",)}),
        _candidate("C", rank=3, sales=80.0, dimensions={"atc4": ("C10A1",), "molecule": ("ezetimibe",), "class": ("스타틴",)}),
        _candidate("D", rank=4, sales=70.0, dimensions={"atc4": ("C10C0",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
    )
    applied = {"atc4": ["C10A1"], "molecule": ["pitavastatin"], "class": ["스타틴", "복합제"]}

    choices = _select_choices(candidates, selected_brand="선택", applied_filter=applied)

    assert [choice.brand_key for choice in choices] == ["선택", "A", "B"]
    assert choices[0].is_selected is True


def test_general_default_filter_applies_market_atc4() -> None:
    assert applied_brand_filter("general", "c10a1", {}) == {"atc4": ["C10A1"]}


def test_general_market_resolution_uses_matching_membership_not_first_requested_atc(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_brand_atc4_values",
        lambda **_kwargs: ("A10N1",),
        raising=False,
    )

    market_id = resolver._resolve_general_market_id(
        selected_brand="가드렛",
        requested_market_id="A10C1",
        filter_payload={"atc4": ["A10C1", "A10C2", "A10N1"]},
        source="iqvia_nsa",
    )

    assert market_id == "A10N1"


def test_general_market_resolution_is_independent_of_requested_atc_order(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_brand_atc4_values",
        lambda **_kwargs: ("A10N1", "A10N3"),
        raising=False,
    )

    results = {
        resolver._resolve_general_market_id(
            selected_brand="가드렛",
            requested_market_id=values[0],
            filter_payload={"atc4": list(values)},
            source="iqvia_nsa",
        )
        for values in (
            ("A10C1", "A10N3", "A10N1"),
            ("A10N3", "A10C1", "A10N1"),
            ("A10N1", "A10N3", "A10C1"),
        )
    }

    assert results == {"A10N1"}


def test_general_market_resolution_preserves_first_candidate_for_all_misses(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_brand_atc4_values",
        lambda **_kwargs: ("A10N1",),
        raising=False,
    )

    market_id = resolver._resolve_general_market_id(
        selected_brand="가드렛",
        requested_market_id="A10C1",
        filter_payload={"atc4": ["A10C1", "A10C2"]},
        source="iqvia_nsa",
    )

    assert market_id == "A10C1"


def test_general_brand_set_resolves_display_alias_to_mart_brand_key(monkeypatch) -> None:
    monkeypatch.setattr(resolver, "_resolve_general_market_id", lambda **_kwargs: "K01D2")
    monkeypatch.setattr(
        resolver,
        "_fetch_brand_rows",
        lambda *_args, **_kwargs: (
            {
                "brand_key": "위너프에이플러스",
                "brand_name": "위너프에이플러스",
                "by_dimension": {"products": []},
            },
        ),
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_market_row",
        lambda *_args, **_kwargs: {"atc4_code": "K01D2", "brand_ranking": {}},
    )
    monkeypatch.setattr(resolver, "_brand_candidates", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(resolver, "_select_choices", lambda *_args, **_kwargs: ())

    result = resolve_brand_set(
        view_name="general",
        market_id="K01D1",
        selected_brand="위너프A+",
        filter_payload={"atc4": ["K01D1", "K01D2"]},
    )

    assert result is not None
    assert result.market_id == "K01D2"
    assert result.selected_brand == "위너프에이플러스"


def test_applied_filter_echoes_audit_code_and_ignores_ubist_channel_axis() -> None:
    payload = {
        "analysis_level": {
            "iqvia": {"audit_code": ["khpa", "KCPA", "khpa"]},
            "ubist": {"facility": ["의원"], "specialty": ["순환기(Cardiology IM)"]},
        }
    }

    applied = applied_brand_filter("general", "C10A1", payload)

    assert applied["atc4"] == ["C10A1"]
    assert applied["channel_axis"] == {"source": "iqvia_nsa", "audit_code": ["KHPA", "KCPA"]}
    assert applied_brand_filter("general", "C10A1", {"analysis_level": {"ubist": {"facility": ["의원"]}}}) == {
        "atc4": ["C10A1"]
    }
    assert "channel_axis" not in applied_brand_filter("strategic_ml", "ml_006", payload)


def test_strategic_cd_uses_the_strategic_ml_filter_dimensions() -> None:
    payload = {"atc4": ["C10A1"], "molecule": ["pitavastatin"], "class": ["Statin"]}

    assert applied_brand_filter("strategic_cd", "cd_006", payload) == applied_brand_filter(
        "strategic_ml", "ml_006", payload
    )


def test_applied_filter_accepts_iqvia_dimension_filters() -> None:
    payload = {
        "analysis_level": {
            "iqvia": {
                "mfr_name_kor": ["제이더블유중외제약"],
                "molecule_type": ["SINGLE"],
                "molecule_desc": ["TOCILIZUMAB"],
                "pack_desc": [" PFS 162MG/0.9ML "],
                "strength": ["162MG"],
                "nhi_type": ["NHI"],
                "audit_code": ["khpa"],
            }
        }
    }

    applied = applied_brand_filter("general", "M01C0", payload)

    assert applied == {
        "atc4": ["M01C0"],
        "mfr": ["제이더블유중외제약"],
        "molecule_type": ["SINGLE"],
        "molecule_desc": ["TOCILIZUMAB"],
        "pack": ["PFS 162MG/0.9ML"],
        "strength": ["162MG"],
        "nhi": ["NHI"],
        "channel_axis": {"source": "iqvia_nsa", "audit_code": ["KHPA"]},
    }


def test_audit_code_sales_value_sums_selected_matrix_codes_for_quarter() -> None:
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["KHPA", "KCPA"]}}})
    row = {
        "audit_code_matrix": {
            "KHPA": {"2026-04": 10, "2026-05": 20},
            "KCPA": {"2026-Q2": 7},
            "KPA": {"2026-Q2": 100},
        }
    }

    assert audit_code_sales_value(row, channel_axis, "2026-Q2") == 37.0


def test_unknown_audit_code_is_rejected_from_dynamic_matrix_keys() -> None:
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["BAD"]}}})

    try:
        validate_audit_code_axis(({"audit_code_matrix": {"KHPA": {"2026-Q2": 1}}},), channel_axis)
    except BrandSetInputError as exc:
        assert "unsupported audit_code" in str(exc)
        assert "BAD" in str(exc)
    else:
        raise AssertionError("unknown audit_code must be rejected")


def test_single_ml_brand_uses_shared_catalog_context_and_ubist_source(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_resolve(**kwargs: object):
        calls.append(kwargs)
        return type(
            "Context",
            (),
            {"market_id": "ml_006", "brand_key": "리바로", "brand_name": "리바로", "db_source": "ubist"},
        )()

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.resolve_deep_analysis_context", fake_resolve)

    context = _resolve_strategic_brand_context("리바로", view_name="strategic_ml", market_id=None)

    assert context.market_id == "ml_006"
    assert context.db_source == "ubist"
    assert calls == [{"brand": "리바로", "view_kind": "strategic_ml", "market_id": None, "source": None}]


def test_single_cd_brand_uses_shared_catalog_context(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_resolve(**kwargs: object):
        calls.append(kwargs)
        return type(
            "Context",
            (),
            {"market_id": "cd_006", "brand_key": "리바로", "brand_name": "리바로", "db_source": "iqvia_nsa"},
        )()

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.resolve_deep_analysis_context", fake_resolve)

    context = _resolve_strategic_brand_context("리바로", view_name="strategic_cd", market_id="cd_006")

    assert context.market_id == "cd_006"
    assert calls == [{"brand": "리바로", "view_kind": "strategic_cd", "market_id": "cd_006", "source": None}]


def test_livalo_strategic_resolution_reads_ubist_mart_rows(monkeypatch) -> None:
    context = type(
        "Context",
        (),
        {"market_id": "ml_006", "brand_key": "리바로", "brand_name": "리바로", "db_source": "ubist"},
    )()
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.resolve_deep_analysis_context",
        lambda **_kwargs: context,
    )

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append(("brand", tuple(params)))
        return [
            {
                "brand_key": "리바로",
                "brand_name": "리바로",
                "is_jw": 1,
                "by_dimension": {"products": [{"product_code": "LIVALO"}]},
                "overlay_data": {},
                "metric_history": {"2026-05": {"rank": 1, "raw_value": 100.0}},
                "audit_code_matrix": None,
            }
        ]

    def fake_fetch_one(_sql: str, params: tuple[object, ...]) -> dict[str, object]:
        calls.append(("market", tuple(params)))
        return {
            "ml_id": "ml_006",
            "ml_name": "리바로 리바로젯",
            "market_size_series": {"2026-05": 100.0},
            "brand_ranking_stacked": {"2026-05": [{"brand_key": "리바로", "rank": 1, "raw_value": 100.0}]},
        }

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_one", fake_fetch_one)

    result = resolve_brand_set(view_name="strategic_ml", market_id=None, selected_brand="리바로")

    assert result is not None
    assert result.market_id == "ml_006"
    assert [choice.brand_key for choice in result.choices] == ["리바로"]
    assert calls == [("brand", ("ml_006", "ubist", "sales")), ("market", ("ml_006", "ubist", "sales"))]


def test_strategic_resolution_can_restrict_rows_to_market_ranking(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_fetch_one(_sql: str, params: tuple[object, ...]) -> dict[str, object]:
        calls.append(("market", params))
        return {
            "ml_id": "ml_003",
            "ml_name": "시장",
            "market_size_series": {},
            "brand_ranking_stacked": {"2026-05": [{"brand_key": f"b{i}", "rank": i} for i in range(1, 7)]},
        }

    def fake_fetch_all(_sql: str, params: tuple[object, ...]) -> tuple[dict[str, object], ...]:
        calls.append(("brand", params))
        assert params[:3] == ("ml_003", "ubist", "sales")
        assert set(params[3:]) == {"선택", "b1", "b2", "b3", "b4", "b5"}
        return tuple(
            {
                "brand_key": key,
                "brand_name": key,
                "is_jw": 1,
                "by_dimension": {"products": []},
                "overlay_data": {},
                "metric_history": {"2026-05": {"rank": rank, "raw_value": 100.0}},
                "audit_code_matrix": None,
            }
            for rank, key in enumerate(("선택", "b1", "b2", "b3", "b4", "b5"), 1)
        )

    context = type("Context", (), {"market_id": "ml_003", "brand_key": "선택", "db_source": "ubist"})()
    monkeypatch.setattr(resolver.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(resolver.db, "fetch_all", fake_fetch_all)
    result = resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_003",
        selected_brand="선택",
        filter_payload={},
        source="ubist",
        rank_by_latest_period=True,
        resolved_context=context,
        restrict_strategic_to_ranking=True,
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["선택", "b1", "b2", "b3", "b4", "b5"]
    assert [kind for kind, _ in calls] == ["market", "brand"]


def test_strategic_resolution_prefilters_exact_multi_period_choices_before_loading_full_rows(monkeypatch) -> None:
    calls: list[tuple[str, str, tuple[object, ...]]] = []
    metric_history = {
        "선택": {"2026-Q1": {"raw_value": 1.0}, "2026-Q2": {"raw_value": 1.0}},
        "b1": {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 100.0}},
        "b2": {"2026-Q1": {"raw_value": 90.0}, "2026-Q2": {"raw_value": 90.0}},
        "b3": {"2026-Q1": {"raw_value": 80.0}, "2026-Q2": {"raw_value": 80.0}},
        "b4": {"2026-Q1": {"raw_value": 70.0}, "2026-Q2": {"raw_value": 70.0}},
        "b5": {"2026-Q1": {"raw_value": 0.0}, "2026-Q2": {"raw_value": 60.0}},
        "b6": {"2026-Q1": {"raw_value": 50.0}, "2026-Q2": {"raw_value": 40.0}},
    }

    monkeypatch.setattr(
        resolver,
        "_resolve_strategic_brand_context",
        lambda *_args, **_kwargs: type(
            "Context",
            (),
            {"market_id": "ml_008", "brand_key": "선택", "db_source": "ubist"},
        )(),
    )

    def fake_fetch_one(sql: str, params: tuple[object, ...]) -> dict[str, object]:
        calls.append(("market", sql, tuple(params)))
        return {
            "ml_id": "ml_008",
            "ml_name": "시장",
            "market_size_series": {},
            "brand_ranking_stacked": {
                "2026-Q2": [
                    {"brand_key": key, "rank": rank}
                    for rank, key in enumerate(("b1", "b2", "b3", "b4", "b5", "b6"), 1)
                ]
            },
        }

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        phase = "full" if "overlay_data" in sql else "light"
        calls.append((phase, sql, tuple(params)))
        if phase == "light":
            assert "overlay_data" not in sql
            assert "raw_value_history" in sql
            assert "metric_history" not in sql
            return [
                {
                    "brand_key": key,
                    "brand_name": key,
                    "is_jw": 1,
                    "product_codes": f'["{key}"]',
                    "raw_value_history": {
                        period: payload["raw_value"]
                        for period, payload in history.items()
                    },
                }
                for key, history in metric_history.items()
            ]
        selected_keys = tuple(str(value) for value in params[3:])
        return [
            {
                "brand_key": key,
                "brand_name": key,
                "is_jw": 1,
                "by_dimension": {"products": [{"product_code": key}]},
                "overlay_data": {},
                "metric_history": metric_history[key],
                "audit_code_matrix": None,
            }
            for key in selected_keys
        ]

    monkeypatch.setattr(resolver.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(resolver.db, "fetch_all", fake_fetch_all)

    result = resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_008",
        selected_brand="선택",
        filter_payload={},
        ranking_quarters=("2026-Q1", "2026-Q2"),
        prefilter_strategic_choices=True,
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["선택", "b1", "b2", "b3", "b4", "b6"]
    assert [phase for phase, _sql, _params in calls] == ["market", "light", "full"]
    assert set(calls[-1][2][3:]) == {"선택", "b1", "b2", "b3", "b4", "b6"}
    assert set(result.brand_meta) == set(metric_history)
    assert result.brand_meta["b5"].product_codes == ("B5",)


def test_strategic_choice_prefilter_falls_back_when_selected_brand_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        resolver,
        "_resolve_strategic_brand_context",
        lambda *_args, **_kwargs: type(
            "Context",
            (),
            {"market_id": "ml_008", "brand_key": "선택", "db_source": "ubist"},
        )(),
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_market_row",
        lambda *_args, **_kwargs: {
            "ml_id": "ml_008",
            "ml_name": "시장",
            "market_size_series": {},
            "brand_ranking_stacked": {
                "2026-Q2": [{"brand_key": "경쟁", "rank": 1}],
            },
        },
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_brand_choice_rows",
        lambda *_args, **_kwargs: [
            {
                "brand_key": "경쟁",
                "brand_name": "경쟁",
                "is_jw": 0,
                "product_codes": ["COMP"],
                "raw_value_history": {"2026-Q2": 100.0},
            }
        ],
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_brand_rows",
        lambda *_args, **_kwargs: [
            {
                "brand_key": "선택",
                "brand_name": "선택",
                "is_jw": 1,
                "by_dimension": {"products": [{"product_code": "SELECTED"}]},
                "overlay_data": {},
                "metric_history": {"2026-Q2": {"raw_value": 1.0}},
                "audit_code_matrix": None,
            },
            {
                "brand_key": "경쟁",
                "brand_name": "경쟁",
                "is_jw": 0,
                "by_dimension": {"products": [{"product_code": "COMP"}]},
                "overlay_data": {},
                "metric_history": {"2026-Q2": {"raw_value": 100.0}},
                "audit_code_matrix": None,
            },
        ],
    )

    result = resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_008",
        selected_brand="선택",
        filter_payload={},
        prefilter_strategic_choices=True,
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["선택", "경쟁"]


def test_strategic_choice_prefilter_is_not_used_for_latest_rank_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        resolver,
        "_resolve_strategic_brand_context",
        lambda *_args, **_kwargs: type(
            "Context",
            (),
            {"market_id": "ml_008", "brand_key": "선택", "db_source": "ubist"},
        )(),
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_market_row",
        lambda *_args, **_kwargs: {
            "ml_id": "ml_008",
            "ml_name": "시장",
            "market_size_series": {},
            "brand_ranking_stacked": {"2026-05": []},
        },
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_brand_choice_rows",
        lambda *_args, **_kwargs: pytest.fail("latest-rank mode must keep the exact legacy path"),
    )
    monkeypatch.setattr(
        resolver,
        "_fetch_brand_rows",
        lambda *_args, **_kwargs: [
            {
                "brand_key": "선택",
                "brand_name": "선택",
                "is_jw": 1,
                "by_dimension": {"products": [{"product_code": "SELECTED"}]},
                "overlay_data": {},
                "metric_history": {"2026-05": {"raw_value": 1.0, "rank": 1}},
                "audit_code_matrix": None,
            }
        ],
    )

    result = resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_008",
        selected_brand="선택",
        filter_payload={},
        rank_by_latest_period=True,
        prefilter_strategic_choices=True,
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["선택"]


def test_ambiguous_ml_brand_preserves_409_context_contract(monkeypatch) -> None:
    error = DeepAnalysisContextError(
        status_code=409,
        error="ambiguous_market_context",
        message="market_id is required because the brand belongs to multiple markets",
        available_contexts=({"view_kind": "strategic_ml", "market_id": "ml_002"}, {"view_kind": "strategic_ml", "market_id": "ml_006"}),
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.resolve_deep_analysis_context",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )

    try:
        _resolve_strategic_brand_context("가드렛", view_name="strategic_ml", market_id=None)
    except BrandSetInputError as exc:
        assert exc.status_code == 409
        assert exc.detail()["available_contexts"] == list(error.available_contexts)
    else:
        raise AssertionError("ambiguous ML brand must return the shared context error")


def test_nonmember_ml_brand_returns_structured_400_detail(monkeypatch) -> None:
    error = DeepAnalysisContextError(
        status_code=404,
        error="brand_not_found",
        message="brand has no serving context for the requested view",
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.resolve_deep_analysis_context",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )

    try:
        _resolve_strategic_brand_context("비소속", view_name="strategic_ml", market_id=None)
    except BrandSetInputError as exc:
        assert exc.status_code == 400
        assert exc.detail() == {
            "error": "brand_not_found",
            "message": "brand has no serving context for the requested view",
            "requested": {"brand": "비소속", "view": "strategic_ml", "market_id": None},
            "hint": "verify strategic catalog membership",
        }
    else:
        raise AssertionError("nonmember ML brand must return a structured input error")


def test_general_market_scope_member_resolves_livalo_to_member_atc4(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    _patch_resolver_db(monkeypatch, calls)

    result = resolve_brand_set(
        view_name="general",
        market_id=None,
        selected_brand="리바로",
        filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        ranking_quarters=("2026-Q2",),
    )

    assert result is not None
    assert result.market_id == "C10A1"
    assert result.applied_filter["atc4"] == ["C10A1"]
    assert ("brand", ("C10A1", "iqvia_nsa", "sales")) in calls
    assert ("market", ("C10A1", "iqvia_nsa", "sales")) in calls


def test_general_market_scope_preserves_explicit_group_atc4_membership(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    _patch_resolver_db(monkeypatch, calls)

    result = resolve_brand_set(
        view_name="general",
        market_id=None,
        selected_brand="리바로",
        filter_payload={
            "atc4": ["C10A1", "C10C0"],
            "market_scope": {"option_id": "group:livalo_family", "member": "리바로"},
        },
        ranking_quarters=("2026-Q2",),
    )

    assert result is not None
    assert result.market_id == "C10A1"
    assert result.applied_filter["atc4"] == ["C10A1", "C10C0"]
    assert ("brand", ("C10A1", "iqvia_nsa", "sales")) in calls
    assert ("market", ("C10A1", "iqvia_nsa", "sales")) in calls


@pytest.mark.parametrize(
    ("member", "expected_market_id"),
    (("가드렛", "A10N1"), ("가드메트", "A10N3")),
)
def test_general_market_scope_resolves_gardlet_family_members(
    member: str,
    expected_market_id: str,
) -> None:
    market_id = resolver._market_scope_market_id(
        view_name="general",
        selected_brand=member,
        filter_payload={
            "atc4": ["A10N1", "A10N3"],
            "market_scope": {"option_id": "group:gardlet_family", "member": member},
        },
    )

    assert market_id == expected_market_id


def test_general_market_scope_member_resolves_livalozet_to_member_atc4(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    _patch_resolver_db(monkeypatch, calls)

    result = resolve_brand_set(
        view_name="general",
        market_id=None,
        selected_brand="리바로젯",
        filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로젯"}},
        ranking_quarters=("2026-Q2",),
    )

    assert result is not None
    assert result.market_id == "C10C0"
    assert result.applied_filter["atc4"] == ["C10C0"]
    assert ("brand", ("C10C0", "iqvia_nsa", "sales")) in calls


def test_general_market_scope_rejects_whole_group_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "전체"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_market_scope_member" in str(exc)
    else:
        raise AssertionError("whole group member must be rejected in Phase 1")


def test_general_market_scope_rejects_missing_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_market_scope_member" in str(exc)
    else:
        raise AssertionError("omitted member must be rejected in Phase 1")


def test_general_market_scope_rejects_unknown_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "존재안함"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope_member" in str(exc)
    else:
        raise AssertionError("unknown member must be rejected")


def test_general_market_scope_rejects_unknown_option(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:not_found", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope" in str(exc)
    else:
        raise AssertionError("unknown option_id must be rejected")


def test_general_market_scope_rejects_non_group_option(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "source:strategy_006", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope" in str(exc)
    else:
        raise AssertionError("non-group option must be rejected")


def test_general_market_scope_rejects_multi_atc4_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="위너프A+",
            filter_payload={"market_scope": {"option_id": "group:winuf_family", "member": "위너프A+"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_member_scope" in str(exc)
    else:
        raise AssertionError("multi-ATC4 member must be rejected in Phase 1")


def test_strategic_ml_market_scope_is_rejected(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="strategic_ml",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_view_for_market_scope" in str(exc)
    else:
        raise AssertionError("strategic_ml market_scope must be rejected")


def test_audit_code_axis_replaces_candidate_sales_ranking_value(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", lambda *_args, **_kwargs: [])
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 1, "rank": 99}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 1}},
        },
        {
            "brand_key": "A",
            "by_dimension": {"products": [{"product_code": "A"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 100, "rank": 1}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 3}},
        },
        {
            "brand_key": "B",
            "by_dimension": {"products": [{"product_code": "B"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 20, "rank": 2}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 50}},
        },
    )
    metas = {
        str(row["brand_key"]): BrandMeta(str(row["brand_key"]), str(row["brand_key"]), (str(row["brand_key"]),), False)
        for row in rows
    }
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["KHPA"]}}})

    candidates = _brand_candidates(
        "general",
        rows,
        metas,
        {"quarter": "2026-Q2", "items": [{"brand_key": "A", "rank": 1, "raw_value": 100}]},
        audit_code_axis=channel_axis,
    )
    choices = _select_choices(candidates, selected_brand="선택", applied_filter={"atc4": ["C10A1"]})

    assert [candidate.sales_value for candidate in candidates] == [1.0, 3.0, 50.0]
    assert [choice.brand_key for choice in choices] == ["선택", "B", "A"]


def test_brand_choices_rank_competitors_by_total_sales_before_snapshot_rank() -> None:
    candidates = (
        _candidate("선택", rank=99, sales=1.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("A", rank=1, sales=10.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("B", rank=5, sales=50.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("C", rank=2, sales=50.0, dimensions={"atc4": ("C10A1",)}),
    )

    choices = _select_choices(candidates, selected_brand="선택", applied_filter={"atc4": ["C10A1"]})

    assert [choice.brand_key for choice in choices] == ["선택", "B", "C", "A"]


def test_brand_choices_can_rank_competitors_by_latest_market_rank() -> None:
    candidates = (
        _candidate("선택", rank=3, sales=1.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("A", rank=1, sales=10.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("B", rank=5, sales=500.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("C", rank=2, sales=50.0, dimensions={"atc4": ("C10A1",)}),
    )

    choices = _select_choices(
        candidates,
        selected_brand="선택",
        applied_filter={"atc4": ["C10A1"]},
        rank_by_latest_period=True,
    )

    assert [choice.brand_key for choice in choices] == ["선택", "A", "C", "B"]
    assert [choice.sales_rank for choice in choices] == [3, 1, 2, 5]


def test_general_brand_candidates_load_iqvia_sidecar_dimensions(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )

    def fake_fetch_all(sql: str, params: tuple) -> list[dict[str, str]]:
        assert "mart_general_filter_dimension_metric" in sql
        return [
            {"brand_key": "A", "dimension_type": "mfr", "dimension_value_norm": "제이더블유중외제약"},
            {"brand_key": "A", "dimension_type": "pack", "dimension_value_norm": "PFS 162MG/0.9ML"},
            {"brand_key": "B", "dimension_type": "mfr", "dimension_value_norm": "경쟁사"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 1, "rank": 99}},
        },
        {
            "brand_key": "A",
            "by_dimension": {"products": [{"product_code": "A"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 100, "rank": 1}},
        },
        {
            "brand_key": "B",
            "by_dimension": {"products": [{"product_code": "B"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 90, "rank": 2}},
        },
    )
    metas = {
        str(row["brand_key"]): BrandMeta(str(row["brand_key"]), str(row["brand_key"]), (str(row["brand_key"]),), False)
        for row in rows
    }

    candidates = _brand_candidates(
        "general",
        rows,
        metas,
        {
            "quarter": "2026-Q2",
            "items": [
                {"brand_key": "A", "rank": 1, "raw_value": 100},
                {"brand_key": "B", "rank": 2, "raw_value": 90},
            ],
        },
    )
    choices = _select_choices(
        candidates,
        selected_brand="선택",
        applied_filter={"atc4": ["M01C0"], "mfr": ["제이더블유중외제약"]},
    )

    assert [candidate.dimensions.get("mfr", ()) for candidate in candidates] == [(), ("제이더블유중외제약",), ("경쟁사",)]
    assert [choice.brand_key for choice in choices] == ["선택", "A"]


def _candidate(brand_key: str, *, rank: int, sales: float, dimensions: dict[str, tuple[str, ...]]) -> BrandCandidate:
    return BrandCandidate(
        meta=BrandMeta(brand_key=brand_key, brand_name=brand_key, product_codes=(brand_key.upper(),), is_jw=False),
        dimensions=dimensions,
        sales_rank=rank,
        sales_value=sales,
    )


def _patch_resolver_db(monkeypatch, calls: list[tuple[str, tuple[object, ...]]]) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        if "mart_general_brand_metric" in sql:
            calls.append(("brand", tuple(params)))
            return [
                _brand_row("리바로", "리바로", "LIVALO", str(params[0]), 1, 100.0),
                _brand_row("리바로젯", "리바로젯", "LIVALOZET", str(params[0]), 2, 80.0),
                _brand_row("경쟁", "경쟁", "COMP", str(params[0]), 3, 50.0),
            ]
        if "mart_general_filter_dimension_metric" in sql:
            return []
        raise AssertionError(f"unexpected sql: {sql}")

    def fake_fetch_one(sql: str, params: tuple[object, ...]) -> dict[str, object]:
        calls.append(("market", tuple(params)))
        return {
            "atc4_code": str(params[0]),
            "atc4_desc": f"Market {params[0]}",
            "brand_ranking": {
                "2026-Q2": [
                    {"brand_key": "리바로", "rank": 1, "raw_value": 100.0},
                    {"brand_key": "리바로젯", "rank": 2, "raw_value": 80.0},
                    {"brand_key": "경쟁", "rank": 3, "raw_value": 50.0},
                ]
            },
        }

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_one", fake_fetch_one)


def _brand_row(
    brand_key: str,
    brand_name: str,
    product_code: str,
    atc4_code: str,
    rank: int,
    sales: float,
) -> dict[str, object]:
    return {
        "brand_key": brand_key,
        "brand_name": brand_name,
        "by_dimension": {"products": [{"product_code": product_code}], "atc4_code": [atc4_code]},
        "metric_history": {"2026-Q2": {"rank": rank, "raw_value": sales}},
        "audit_code_matrix": {},
    }
