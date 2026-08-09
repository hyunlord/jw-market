"""F-055: cold-latency regression tests for indexed brand lookups and scan reuse."""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.scripts.api import brand_activity_brand_molecules as molecules
from pipeline.scripts.api import brand_activity_brand_resolver as resolver
from pipeline.scripts.api import deep_analysis_context
from pipeline.scripts.api import deep_analysis_runtime
from pipeline.scripts.api.deep_analysis_serving import ForecastBlock
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext, DeepAnalysisSource
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta


def _context(
    *,
    brand: str = "선택브랜드",
    source: DeepAnalysisSource = "ubist",
    market_allowed_sources: tuple[DeepAnalysisSource, ...] = ("ubist",),
    db_source: str = "ubist",
) -> DeepAnalysisContext:
    return DeepAnalysisContext(
        brand_key=brand,
        brand_name=brand,
        view_kind="strategic_ml",
        market_id="ml_003",
        market_name="당뇨 OAD",
        source=source,
        db_source=db_source,
        in_catalog=True,
        has_market_data=True,
        market_allowed_sources=market_allowed_sources,
        brand_available_sources=("ubist",),
    )


def test_general_rows_key_hit_uses_single_indexed_query(monkeypatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def fake_fetch_all(sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append((sql, tuple(params)))
        return [{"brand_key": "리바로", "brand_name": "리바로", "atc4_code": "C10A1", "market_name": "지질", "source": "ubist"}]

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)
    rows = deep_analysis_context._general_rows("리바로")
    assert rows and rows[0]["brand_key"] == "리바로"
    assert len(calls) == 1
    sql, params = calls[0]
    assert "brand_key = %s" in sql
    assert " OR " not in sql
    assert params == ("리바로",)


def test_general_rows_key_miss_falls_back_to_brand_name(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        if "brand_name = %s" in sql:
            return [{"brand_key": "K", "brand_name": "이름", "atc4_code": "A", "market_name": None, "source": "ubist"}]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)
    rows = deep_analysis_context._general_rows("이름")
    assert rows and rows[0]["brand_name"] == "이름"
    assert "brand_key = %s" in calls[0]
    assert "brand_name = %s" in calls[1]
    assert all(" OR " not in sql for sql in calls)


def test_brand_available_sources_key_hit_short_circuits(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        return [{"source": "ubist"}, {"source": "iqvia_nsa"}]

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)
    sources = deep_analysis_context._brand_available_sources("브랜드", "키", "이름")
    assert sources == ("ubist", "iqvia_nsa")
    assert len(calls) == 1
    assert "brand_key IN" in calls[0]
    assert " OR " not in calls[0]


def test_brand_available_sources_key_miss_falls_back(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        if "brand_name IN" in sql:
            return [{"source": "iqvia_nsa"}]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)
    sources = deep_analysis_context._brand_available_sources("브랜드", "키", "이름")
    assert sources == ("iqvia_nsa",)
    assert len(calls) == 2


def test_general_atc_only_candidates_skip_molecule_and_sidecar_bridges(monkeypatch) -> None:
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["C10C0"]},
            "metric_history": {"2026-Q1": {"raw_value": 10, "rank": 1}},
        },
    )
    metas = {"선택": BrandMeta("선택", "선택", ("SEL",), False)}
    monkeypatch.setattr(
        resolver,
        "general_molecules_by_product",
        lambda _metas: pytest.fail("atc-only resolution must not load the global molecule bridge"),
    )
    monkeypatch.setattr(
        resolver,
        "_general_sidecar_dimensions",
        lambda _rows: pytest.fail("atc-only resolution must not load IQVIA sidecar dimensions"),
    )

    candidates = resolver._brand_candidates(
        "general",
        rows,
        metas,
        {"quarter": "2026-Q1", "items": []},
        source="iqvia_nsa",
        required_dimensions={"atc4": ["C10C0"]},
    )

    assert len(candidates) == 1
    assert candidates[0].dimensions == {"atc4": ("C10C0",), "molecule": ()}


def test_general_molecule_filter_still_loads_molecule_bridge(monkeypatch) -> None:
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["C10C0"]},
            "metric_history": {"2026-Q1": {"raw_value": 10, "rank": 1}},
        },
    )
    metas = {"선택": BrandMeta("선택", "선택", ("SEL",), False)}
    calls: list[dict[str, BrandMeta]] = []
    monkeypatch.setattr(resolver, "general_molecules_by_product", lambda value: calls.append(value) or {"SEL": ("성분",)})
    monkeypatch.setattr(
        resolver,
        "_general_sidecar_dimensions",
        lambda _rows: pytest.fail("molecule-only resolution must not load sidecar dimensions"),
    )

    candidates = resolver._brand_candidates(
        "general",
        rows,
        metas,
        {"quarter": "2026-Q1", "items": []},
        source="iqvia_nsa",
        required_dimensions={"atc4": ["C10C0"], "molecule": ["성분"]},
    )

    assert calls == [metas]
    assert candidates[0].dimensions["molecule"] == ("성분",)


def test_strategic_context_candidate_cache_reuses_shared_queries(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, _params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        if "catalog_strategic_brand" in sql:
            return [{"brand_key": "선택브랜드", "brand_name": "선택브랜드", "market_id": "ml_003", "market_name": "당뇨", "data_source": "both"}]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"market_id": "ml_003", "source": "ubist"}, {"market_id": "ml_003", "source": "iqvia_nsa"}]
        if "mart_general_brand_metric" in sql:
            return [{"source": "ubist"}, {"source": "iqvia_nsa"}]
        raise AssertionError(sql)

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)
    cache: dict[str, Any] = {}
    first = deep_analysis_context.resolve_deep_analysis_context(
        brand="선택브랜드", view_kind="strategic_ml", market_id="ml_003", source="ubist", _candidate_cache=cache
    )
    second = deep_analysis_context.resolve_deep_analysis_context(
        brand="선택브랜드", view_kind="strategic_ml", market_id="ml_003", source="iqvia", _candidate_cache=cache
    )

    assert first.source == "ubist"
    assert second.source == "iqvia"
    assert len(calls) == 3


def test_resolve_brand_set_reuses_resolved_context(monkeypatch) -> None:
    def forbidden_resolve(**kwargs: Any) -> DeepAnalysisContext:
        raise AssertionError("resolve_deep_analysis_context must not re-run with resolved_context")

    captured: dict[str, Any] = {}

    def fake_fetch_brand_rows(view: Any, market_id: str, *, source: str) -> tuple:
        captured["market_id"] = market_id
        captured["source"] = source
        return ()

    monkeypatch.setattr(resolver, "resolve_deep_analysis_context", forbidden_resolve)
    monkeypatch.setattr(resolver, "_fetch_brand_rows", fake_fetch_brand_rows)
    result = resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_003",
        selected_brand="선택브랜드",
        filter_payload={},
        source="ubist",
        resolved_context=_context(),
    )
    assert result is None
    assert captured == {"market_id": "ml_003", "source": "ubist"}


@pytest.mark.parametrize(
    ("brand", "source", "db_source"),
    (
        ("가드렛", "ubist", "ubist"),
        ("리바로", "iqvia", "iqvia_nsa"),
        ("마운자로", "ubist", "ubist"),
    ),
)
def test_resolve_brand_set_multi_source_context_preserves_source(
    monkeypatch,
    brand: str,
    source: DeepAnalysisSource,
    db_source: str,
) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_brand_rows(view: Any, market_id: str, *, source: str) -> tuple:
        captured["source"] = source
        return ()

    monkeypatch.setattr(resolver, "_fetch_brand_rows", fake_fetch_brand_rows)
    resolver.resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_003",
        selected_brand=brand,
        filter_payload={},
        source=source,
        resolved_context=_context(
            brand=brand,
            source=source,
            db_source=db_source,
            market_allowed_sources=("ubist", "iqvia"),
        ),
    )
    assert captured["source"] == db_source


def test_resolve_brand_set_source_is_stable_across_alternating_calls(monkeypatch) -> None:
    captured_sources: list[str] = []
    cases: tuple[tuple[str, DeepAnalysisSource, str], ...] = (
        ("리바로", "iqvia", "iqvia_nsa"),
        ("가드렛", "ubist", "ubist"),
        ("마운자로", "iqvia", "iqvia_nsa"),
    )

    def fake_fetch_brand_rows(view: Any, market_id: str, *, source: str) -> tuple:
        captured_sources.append(source)
        return ()

    monkeypatch.setattr(resolver, "_fetch_brand_rows", fake_fetch_brand_rows)
    for brand, source, db_source in cases:
        resolver.resolve_brand_set(
            view_name="strategic_ml",
            market_id="ml_003",
            selected_brand=brand,
            filter_payload={},
            source=source,
            resolved_context=_context(
                brand=brand,
                source=source,
                db_source=db_source,
                market_allowed_sources=("ubist", "iqvia"),
            ),
        )

    assert captured_sources == ["iqvia_nsa", "ubist", "iqvia_nsa"]


def test_fetch_raw_molecules_loads_latest_quarter_once(monkeypatch) -> None:
    molecules._fetch_raw_molecules.cache_clear()
    molecules._latest_quarter_molecule_pairs.cache_clear()
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        return [
            {"product_code": "A", "molecule": "M1"},
            {"product_code": "B", "molecule": "M2"},
            {"product_code": None, "molecule": "M3"},
        ]

    monkeypatch.setattr(molecules.db, "fetch_all", fake_fetch_all)
    try:
        first = molecules._fetch_raw_molecules(("A",))
        second = molecules._fetch_raw_molecules(("B", "C"))
        assert first == [{"product_code": "A", "molecule": "M1"}]
        assert second == [{"product_code": "B", "molecule": "M2"}]
        assert len(calls) == 1
        assert "IN (" not in calls[0]
    finally:
        molecules._fetch_raw_molecules.cache_clear()
        molecules._latest_quarter_molecule_pairs.cache_clear()


def test_strategic_section_cache_hit_skips_full_market_rows(monkeypatch) -> None:
    brand_row = {
        "brand_name": "리바로",
        "ml_id": "ml_006",
        "source": "ubist",
        "measure": "sales",
        "is_jw": 1,
        "is_target": 1,
    }
    block = ForecastBlock(
        forecast={"by_combo": {"UBIST.sales": {"brand": "리바로"}}},
        simulation={"by_combo": {}},
        generation_status="generated",
        no_history_fallback=None,
    )

    monkeypatch.setattr(deep_analysis_runtime, "_brand_rows", lambda _brand: [brand_row])
    monkeypatch.setattr(deep_analysis_runtime, "_market_catalog", lambda _ml_id: {"name": "지질", "data_source": "ubist"})
    monkeypatch.setattr(deep_analysis_runtime, "_event_payload", lambda _brand: {"cut_a": [], "cut_b": []})
    monkeypatch.setattr(deep_analysis_runtime.builder, "atc_codes_from_market_catalog", lambda _market: ["C10A1"])
    monkeypatch.setattr(deep_analysis_runtime.builder, "source_list", lambda _source: ["UBIST"])
    monkeypatch.setattr(deep_analysis_runtime, "load_forecast_block_by_key", lambda **_kwargs: block)

    row = deep_analysis_runtime.build_strategic_row("리바로")

    assert row is not None
    assert row["market_id"] == "strategy_006"
