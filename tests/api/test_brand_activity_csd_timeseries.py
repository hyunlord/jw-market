from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_csd_timeseries as service
from pipeline.scripts.api import brand_activity_csd_shared as shared
from pipeline.scripts.api.routes import brand_activity


def test_period_ym_to_quarter_handles_boundaries() -> None:
    assert shared.period_ym_to_quarter("2025-03") == "2025-Q1"
    assert shared.period_ym_to_quarter("2025-04") == "2025-Q2"
    assert shared.period_ym_to_quarter("2025-09") == "2025-Q3"
    assert shared.period_ym_to_quarter("2025-10") == "2025-Q4"
    assert shared.period_ym_to_quarter("2025-12") == "2025-Q4"


def test_full_csd_quarters_excludes_partial_edges() -> None:
    months = ["2023-05", "2023-06", "2023-07", "2023-08", "2023-09", "2025-10", "2025-11", "2025-12"]

    assert service.full_quarters_from_months(months) == ["2023-Q3", "2025-Q4"]


def test_months_in_quarter_window_preserves_month_keys() -> None:
    months = ["2025-01", "2025-02", "2025-03", "2025-04"]

    assert shared.months_in_quarter_window(months, ["2025-Q1"]) == ("2025-01", "2025-02", "2025-03")


def test_select_ranked_brands_keeps_selected_and_fills_to_six() -> None:
    ranking = [
        {"brand_key": "A", "brand": "A", "rank": 1},
        {"brand_key": "selected", "brand": "selected", "rank": 2},
        {"brand_key": "B", "brand": "B", "rank": 3},
        {"brand_key": "C", "brand": "C", "rank": 4},
        {"brand_key": "D", "brand": "D", "rank": 5},
        {"brand_key": "E", "brand": "E", "rank": 6},
        {"brand_key": "F", "brand": "F", "rank": 7},
    ]

    selected = shared.select_ranked_brands(ranking, selected_brand="selected")

    assert [brand.brand_key for brand in selected] == ["selected", "A", "B", "C", "D", "E"]
    assert selected[0].is_selected is True
    assert selected[0].sales_rank == 2


def test_normalized_product_overlap_respects_alias_rules() -> None:
    overlap = service.normalized_product_overlap({"A-PITO", "TENELIA"}, {"APITO", "TENELA"})

    assert overlap == {"APITO"}


def test_csd_market_resolution_requires_selected_brand_membership(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "Selected Market", "master_product": "SELECTED"},
            {"market": "Selected Market", "master_product": "RIVAL_A"},
            {"market": "Competitor Market", "master_product": "RIVAL_A"},
            {"market": "Competitor Market", "master_product": "RIVAL_B"},
            {"market": "Competitor Market", "master_product": "RIVAL_C"},
        ],
    )

    resolved = service.resolve_csd_market(
        selected_product_codes={"SELECTED"},
        candidate_product_codes={"SELECTED", "RIVAL_A", "RIVAL_B", "RIVAL_C"},
    )

    assert resolved.market == "Selected Market"
    assert resolved.overlap == ("RIVAL_A", "SELECTED")


def test_csd_market_resolution_limits_scan_to_configured_product_variants(monkeypatch) -> None:
    # Given
    captured: dict[str, str | tuple[str, ...] | None] = {}

    def fetch_all(sql: str, params: tuple[str, ...] | None = None) -> list[dict[str, str]]:
        captured["sql"] = sql
        captured["params"] = params
        return [
            {"market": "Selected Market", "master_product": "A-PITO"},
            {"market": "Selected Market", "master_product": "LOWOSMOPERI"},
        ]

    monkeypatch.setattr(service.db, "fetch_all", fetch_all)

    # When
    resolved = service.resolve_csd_market(
        selected_product_codes={"APITO"},
        candidate_product_codes={"APITO", "LOW OSMO PERI"},
    )

    # Then
    assert "master_product IN (%s, %s, %s, %s)" in str(captured["sql"])
    assert captured["params"] == ("A-PITO", "APITO", "LOW OSMO PERI", "LOWOSMOPERI")
    assert resolved.market == "Selected Market"
    assert resolved.overlap == ("APITO", "LOW OSMO PERI")


def test_csd_product_codes_are_reloaded_from_iqvia_for_ubist_brand_meta(monkeypatch) -> None:
    brand_meta = {
        "리바로": shared.BrandMeta("리바로", "리바로", ("UBIST-LIVALO",), True),
        "경쟁품": shared.BrandMeta("경쟁품", "경쟁품", ("UBIST-RIVAL",), False),
    }
    captured: dict[str, object] = {}

    def fake_iqvia_codes(brands: dict[str, str]) -> dict[str, tuple[str, ...]]:
        captured["brands"] = brands
        return {"리바로": ("LIVALO",), "경쟁품": ("IQVIA-RIVAL",)}

    monkeypatch.setattr(service, "iqvia_product_codes_by_brand", fake_iqvia_codes)

    resolved = service._iqvia_csd_product_codes(brand_meta, selected_brand="리바로")

    assert captured["brands"] == {"리바로": "리바로", "경쟁품": "경쟁품"}
    assert resolved.selected == frozenset({"LIVALO"})
    assert resolved.candidates == frozenset({"LIVALO", "IQVIA-RIVAL"})
    assert resolved.by_brand == {
        "리바로": frozenset({"LIVALO"}),
        "경쟁품": frozenset({"IQVIA-RIVAL"}),
    }


def test_csd_activity_join_uses_iqvia_codes_instead_of_ubist_codes(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params: [
            {"period_ym": "2025-01", "master_product": "LIVALO", "value": 17.0},
        ],
    )

    activity = service._activity_series(
        "LIVALO Market",
        [shared.BrandChoice("리바로", "리바로", 1, True)],
        {"리바로": frozenset({"LIVALO"})},
        ("2025-01",),
    )

    assert activity["by_brand"]["리바로"] == {"2025-01": 17.0}


def test_csd_activity_join_preserves_shared_product_mappings(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params: [
            {"period_ym": "2025-01", "master_product": "SHARED", "value": 7.0},
        ],
    )

    activity = service._activity_series(
        "SHARED Market",
        [
            shared.BrandChoice("brand-a", "Brand A", 1, True),
            shared.BrandChoice("brand-b", "Brand B", 2, False),
        ],
        {
            "brand-a": frozenset({"SHARED"}),
            "brand-b": frozenset({"SHARED"}),
            "not-returned": frozenset({"SHARED"}),
        },
        ("2025-01",),
    )

    assert activity["by_brand"] == {
        "brand-a": {"2025-01": 7.0},
        "brand-b": {"2025-01": 7.0},
    }
    assert activity["matched"] == {"brand-a": True, "brand-b": True}


def test_resolve_csd_markets_excludes_competitor_only_markets(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "LIVALO Market", "master_product": "RIVAL"},
            {"market": "LIVALO FENO Market", "master_product": "LIVALO FENO"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO", "LIVALO FENO", "RIVAL"},
    )

    assert [item.market for item in resolved] == ["LIVALO Market"]


def test_resolve_csd_markets_keeps_legacy_selected_market_as_primary(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "Selected Market", "master_product": "SELECTED"},
            {"market": "Selected Market", "master_product": "RIVAL_A"},
            {"market": "Competitor Market", "master_product": "RIVAL_A"},
            {"market": "Competitor Market", "master_product": "RIVAL_B"},
            {"market": "Competitor Market", "master_product": "RIVAL_C"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"SELECTED"},
        candidate_product_codes={"SELECTED", "RIVAL_A", "RIVAL_B", "RIVAL_C"},
    )

    assert [item.market for item in resolved] == ["Selected Market"]

    with pytest.raises(service.CsdMarketFilterError):
        service._select_csd_markets(resolved, "Competitor Market")


def test_aggregate_csd_markets_uses_period_union_without_zero_fill() -> None:
    aggregate = service._aggregate_market_activity(
        {
            "A": {
                "totals": {"2024-01": 10.0, "2025-01": 20.0},
                "by_brand": {"brand": {"2024-01": 4.0, "2025-01": 8.0}},
            },
            "B": {
                "totals": {"2025-01": 3.0},
                "by_brand": {"brand": {"2025-01": 1.0}},
            },
        }
    )

    assert aggregate["series"]["market_totals"] == {"2024-01": 10.0, "2025-01": 23.0}
    assert aggregate["series"]["by_entity"]["brand"] == {"2024-01": 4.0, "2025-01": 9.0}
    assert aggregate["contributing_markets_by_period"] == {
        "2024-01": ["A"],
        "2025-01": ["A", "B"],
    }
    assert "2024-01" not in aggregate["series_by_market"]["B"]["market_totals"]


def test_csd_market_resolution_rejects_competitor_only_overlap(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [{"market": "Competitor Market", "master_product": "RIVAL"}],
    )

    try:
        service.resolve_csd_market(
            selected_product_codes={"SELECTED"},
            candidate_product_codes={"SELECTED", "RIVAL"},
        )
    except shared.CsdTimeseriesNoMappingError as exc:
        assert str(exc) == "이 브랜드는 CSD 원천에 활동 데이터가 없음"
        assert exc.csd_source_present is False
    else:
        raise AssertionError("competitor-only overlap must not select a CSD market")


def test_csd_market_resolution_exposes_true_tie_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "Alpha Market", "master_product": "SELECTED"},
            {"market": "Beta Market", "master_product": "SELECTED"},
        ],
    )

    try:
        service.resolve_csd_market(
            selected_product_codes={"SELECTED"},
            candidate_product_codes={"SELECTED"},
        )
    except shared.CsdTimeseriesAmbiguousMarketError as exc:
        assert [item["market"] for item in exc.candidates] == ["Alpha Market", "Beta Market"]
    else:
        raise AssertionError("a true top-score tie must remain ambiguous")


def test_csd_timeseries_route_wraps_success_envelope(monkeypatch) -> None:
    expected = {
        "scope": {
            "view": "general",
            "market_id": "C10A1",
            "market_name": "C10A1",
            "csd_market": "LIVALO",
            "selected_brand": {"brand_key": "리바로", "product_code": "LIVALO"},
            "ranking_measure": "sales",
            "ranking_quarter": "2025-Q4",
            "filter": {},
            "quarters": ["2025-Q4"],
            "measures": ["activity", "sales", "unit", "counting_unit", "dosage_unit"],
        },
        "brands": [],
        "market_totals": {},
    }
    captured: dict[str, object] = {}

    def fake_get_csd_timeseries(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return expected

    monkeypatch.setattr(brand_activity, "get_csd_timeseries", fake_get_csd_timeseries)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-timeseries",
        json={
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc": {"atc4": ["C10A1"]}, "analysis_level": {"iqvia": {"audit_code": ["KHPA"]}}},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"data": expected, "meta": {"request_normalized": True}}
    assert "market_id" not in captured
    assert captured["filters"]["atc4"] == ["C10A1"]
    assert captured["filters"]["analysis_level"] == {"iqvia": {"audit_code": ["KHPA"]}}
    assert captured["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_csd_timeseries_route_returns_422_for_unknown_csd_market(monkeypatch) -> None:
    def reject(_payload: dict[str, object]) -> None:
        raise service.CsdMarketFilterError("UNKNOWN", available=("LIVALO",))

    monkeypatch.setattr(brand_activity, "get_csd_timeseries", reject)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-timeseries",
        json={
            "view": "general",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"]},
            "csd_market": "UNKNOWN",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "invalid_csd_market",
        "message": "unsupported csd_market: UNKNOWN",
        "available": ["LIVALO"],
    }


def test_csd_timeseries_route_ignores_stale_market_id_input(monkeypatch) -> None:
    monkeypatch.setattr(brand_activity, "get_csd_timeseries", lambda _payload: None)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-timeseries",
        json={"view": "general", "market_id": "missing", "selected_brand": "리바로", "filter": {}},
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "error": "market_not_found",
            "message": "요청 필터로 시장을 식별할 수 없음",
            "requested": {"view": "general", "filters_received": {}},
            "hint": "flat filters.atc4 or market_id expected",
        }
    }


def test_csd_timeseries_service_uses_select_only_sql() -> None:
    source = Path("pipeline/scripts/api/brand_activity_csd_timeseries.py").read_text()
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ", "TRUNCATE ", "REPLACE ")

    assert not any(token in source.upper() for token in forbidden)


def test_csd_timeseries_parse_accepts_general_market_scope_without_atc4() -> None:
    parsed = service._parse_request(
        {
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        }
    )

    assert parsed["market_id"] is None
    assert parsed["filter"]["market_scope"] == {"option_id": "group:livalo_family", "member": "리바로"}


def test_csd_timeseries_parse_accepts_strategic_cd_market_id() -> None:
    parsed = service._parse_request(
        {
            "view": "strategic_cd",
            "market_id": "cd_006",
            "selected_brand": "리바로",
            "filters": {},
        }
    )

    assert parsed["view"] == "strategic_cd"
    assert parsed["market_id"] == "cd_006"


def test_csd_timeseries_parse_preserves_optional_csd_market() -> None:
    parsed = service._parse_request(
        {
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "csd_market": "LIVALO FENO",
        }
    )

    assert parsed["csd_market"] == "LIVALO FENO"


def test_csd_timeseries_public_measures_include_sales() -> None:
    assert shared.RX_MEASURES == ("sales", "unit", "counting_unit", "dosage_unit")
    assert shared.PUBLIC_MEASURES == ("activity", "sales", "unit", "counting_unit", "dosage_unit")


def test_csd_timeseries_fetches_sales_with_dynamic_measure_placeholders(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(service.db, "fetch_all", fake_fetch_all)

    service._fetch_rx_rows(
        shared.ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_name", "sales_rank", True),
        "C10A1",
        ("리바로",),
    )

    assert "measure IN (%s, %s, %s, %s)" in str(captured["sql"])
    assert captured["params"] == ("C10A1", shared.SOURCE, "sales", "unit", "counting_unit", "dosage_unit", "리바로")


def test_csd_timeseries_market_totals_include_sales(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        assert "measure IN (%s, %s, %s, %s)" in sql
        assert params == ("C10A1", shared.SOURCE, "sales", "unit", "counting_unit", "dosage_unit")
        return [
            {"measure": "sales", "market_size_series": {"2025-Q1": 1200.0}},
            {"measure": "unit", "market_size_series": {"2025-Q1": 300.0}},
        ]

    monkeypatch.setattr(service.db, "fetch_all", fake_fetch_all)

    totals = service._market_totals(
        shared.ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_name", "sales_rank", True),
        "C10A1",
        ["2025-Q1"],
        {"2025-Q1": 44.0},
    )

    assert totals["sales"] == {"2025-Q1": 1200.0}
    assert totals["unit"] == {"2025-Q1": 300.0}
    assert totals["counting_unit"] == {"2025-Q1": 0.0}
    assert totals["dosage_unit"] == {"2025-Q1": 0.0}


def test_csd_timeseries_activity_preserves_monthly_keys(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        assert "GROUP BY period_ym, master_product" in sql
        assert params == ("LIVALO Market",)
        return [
            {"period_ym": "2025-01", "master_product": "LIVALO", "value": 10.0},
            {"period_ym": "2025-02", "master_product": "LIVALO", "value": 20.0},
            {"period_ym": "2025-03", "master_product": "OTHER", "value": 30.0},
            {"period_ym": "2025-04", "master_product": "LIVALO", "value": 40.0},
        ]

    monkeypatch.setattr(service.db, "fetch_all", fake_fetch_all)
    months = ("2025-01", "2025-02", "2025-03")
    activity = service._activity_series(
        "LIVALO Market",
        [shared.BrandChoice("LIVALO", "LIVALO", 1, True)],
        {"LIVALO": frozenset({"LIVALO"})},
        months,
    )

    assert activity["totals"] == {"2025-01": 10.0, "2025-02": 20.0, "2025-03": 30.0}
    assert activity["by_brand"]["LIVALO"] == {"2025-01": 10.0, "2025-02": 20.0, "2025-03": 0.0}
    assert service._activity_payload("LIVALO", activity, months) == {
        "source": "csd",
        "absolute": {"2025-01": 10.0, "2025-02": 20.0, "2025-03": 0.0},
        "ratio": {"2025-01": 100.0, "2025-02": 100.0, "2025-03": 0.0},
    }


def test_csd_timeseries_scope_keeps_quarters_and_adds_activity_months() -> None:
    view = shared.ViewConfig("brand", "market", "atc4_code", "atc4_name", "sales_rank", True)
    payload = service._scope_payload(
        {"view": "general", "market_id": "C10A1", "filter": {}, "mode": "absolute"},
        view,
        {"atc4_code": "C10A1", "atc4_name": "LIVALO"},
        shared.BrandMeta("LIVALO", "LIVALO", ("LIVALO",), True),
        "2025-Q1",
        {},
        shared.CsdCrosswalk("LIVALO Market", "LIVALO", ("LIVALO",), 1),
        (shared.CsdCrosswalk("LIVALO Market", "LIVALO", ("LIVALO",), 1),),
        ["2025-Q1"],
        ("2025-01", "2025-02", "2025-03"),
    )

    assert payload["quarters"] == ["2025-Q1"]
    assert payload["activity_months"] == ["2025-01", "2025-02", "2025-03"]


def test_resolve_csd_markets_includes_same_ml_franchise_sibling(monkeypatch) -> None:
    # ml_id franchise gate: LIVALOZET sheet qualifies via sibling LIVALOZET even though
    # the selected brand LIVALO is not present in that sheet. DONG_KOOK (not a market
    # member / not in candidate) does not create qualification on its own.
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
            {"market": "LIVALOZET Market", "master_product": "DONG_KOOK"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO", "LIVALOZET"},
        qualifying_product_codes={"LIVALO", "LIVALOZET"},
    )

    assert [item.market for item in resolved] == ["LIVALO Market", "LIVALOZET Market"]
    assert resolved[0].market == "LIVALO Market"  # primary anchored on the selected brand


def test_resolve_csd_markets_franchise_primary_stays_selected_anchor(monkeypatch) -> None:
    # Even when a sibling sheet outscores the selected brand's sheet, the primary stays
    # the selected brand's market (label unchanged).
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
            {"market": "LIVALOZET Market", "master_product": "LIVALO_V"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO", "LIVALOZET", "LIVALO_V"},
        qualifying_product_codes={"LIVALO", "LIVALOZET", "LIVALO_V"},
    )

    assert resolved[0].market == "LIVALO Market"  # not the higher-scored LIVALOZET Market
    assert {item.market for item in resolved} == {"LIVALO Market", "LIVALOZET Market"}


def test_resolve_csd_markets_franchise_excludes_nonmember_competitor(monkeypatch) -> None:
    # 6779da0b intent preserved: a sheet whose only product is a non-member competitor
    # (CRESTOR not in the market brand set) never qualifies.
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "CRESTOR Market", "master_product": "CRESTOR"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO", "LIVALOZET"},
        qualifying_product_codes={"LIVALO", "LIVALOZET"},
    )

    assert [item.market for item in resolved] == ["LIVALO Market"]


def test_resolve_csd_markets_default_gate_is_legacy_selected_only(monkeypatch) -> None:
    # Without qualifying_product_codes (general / non-ml_id views) the legacy
    # selected-brand-membership gate is preserved: the sibling sheet is excluded.
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
        ],
    )

    resolved = service.resolve_csd_markets(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO", "LIVALOZET"},
    )

    assert [item.market for item in resolved] == ["LIVALO Market"]


def _franchise_fixture():
    csd_codes = service.CsdProductCodes(
        selected=frozenset({"LIVALO"}),
        candidates=frozenset({"LIVALO", "LIVALOZET", "CRESTOR"}),
        by_brand={
            "리바로": frozenset({"LIVALO"}),
            "리바로젯": frozenset({"LIVALOZET"}),
            "크레스토": frozenset({"CRESTOR"}),
        },
    )
    brand_meta = {
        "리바로": shared.BrandMeta("리바로", "리바로", ("LIVALO",), True),
        "리바로젯": shared.BrandMeta("리바로젯", "리바로젯", ("LIVALOZET",), True),
        "크레스토": shared.BrandMeta("크레스토", "크레스토", ("CRESTOR",), False),
    }
    return csd_codes, brand_meta


def test_franchise_qualifying_codes_includes_same_ml_is_jw_sibling() -> None:
    csd_codes, brand_meta = _franchise_fixture()
    codes = service._franchise_qualifying_codes(csd_codes, brand_meta)
    assert "LIVALO" in codes  # selected
    assert "LIVALOZET" in codes  # same-ml_id JW sibling


def test_franchise_qualifying_codes_excludes_non_jw_same_ml_competitor() -> None:
    # ★ core defense of this change: a same-ml_id member with is_jw=False must not
    # contribute its product codes to the qualification signal.
    csd_codes, brand_meta = _franchise_fixture()
    codes = service._franchise_qualifying_codes(csd_codes, brand_meta)
    assert "CRESTOR" not in codes


def test_franchise_qualifying_codes_keeps_selected_brand_even_if_non_jw() -> None:
    # Selected brand's own codes are always included so its own sheet stays resolvable,
    # even when the selected brand itself is is_jw=False.
    csd_codes = service.CsdProductCodes(
        selected=frozenset({"GENERICX"}),
        candidates=frozenset({"GENERICX", "LIVALO"}),
        by_brand={"제네릭엑스": frozenset({"GENERICX"}), "리바로": frozenset({"LIVALO"})},
    )
    brand_meta = {
        "제네릭엑스": shared.BrandMeta("제네릭엑스", "제네릭엑스", ("GENERICX",), False),
        "리바로": shared.BrandMeta("리바로", "리바로", ("LIVALO",), True),
    }
    codes = service._franchise_qualifying_codes(csd_codes, brand_meta)
    assert "GENERICX" in codes


def test_resolve_csd_markets_isjw_filter_blocks_same_ml_competitor_sheet(monkeypatch) -> None:
    # End-to-end: CRESTOR is a same-ml_id member (in candidate) with its own sheet, but
    # is_jw-filtered qualifying excludes it, so CRESTOR Market does not qualify while the
    # JW sibling LIVALOZET Market does.
    monkeypatch.setattr(
        service.db,
        "fetch_all",
        lambda _sql, _params=None: [
            {"market": "LIVALO Market", "master_product": "LIVALO"},
            {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
            {"market": "CRESTOR Market", "master_product": "CRESTOR"},
        ],
    )
    csd_codes, brand_meta = _franchise_fixture()
    qualifying = service._franchise_qualifying_codes(csd_codes, brand_meta)

    resolved = service.resolve_csd_markets(
        selected_product_codes=set(csd_codes.selected),
        candidate_product_codes=set(csd_codes.candidates),
        qualifying_product_codes=qualifying,
    )

    assert [item.market for item in resolved] == ["LIVALO Market", "LIVALOZET Market"]
    assert resolved[0].market == "LIVALO Market"  # primary anchor unchanged


def test_general_franchise_codes_includes_same_ml_jw_sibling(monkeypatch) -> None:
    # 리바로 -> ml_006; canonical registry ml_006 = {리바로, 리바로젯} (JW franchise).
    monkeypatch.setattr(
        service,
        "iqvia_product_codes_by_brand",
        lambda names: {"리바로": ("LIVALO",), "리바로젯": ("LIVALOZET",)},
    )
    codes = service._general_franchise_codes("리바로", {"LIVALO"})
    assert "LIVALO" in codes and "LIVALOZET" in codes


def test_general_franchise_codes_scopes_to_selected_ml_only(monkeypatch) -> None:
    # 리바로페노=ml_007, 리바로하이/브이=ml_008 must NOT join 리바로(ml_006) franchise.
    captured: dict[str, set[str]] = {}

    def fake(names):
        captured["names"] = set(names.values())
        return {name: () for name in names}

    monkeypatch.setattr(service, "iqvia_product_codes_by_brand", fake)
    service._general_franchise_codes("리바로", {"LIVALO"})
    assert captured["names"] == {"리바로", "리바로젯"}


def test_general_franchise_codes_unmapped_brand_falls_back_no_query(monkeypatch) -> None:
    calls = {"n": 0}

    def fake(names):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(service, "iqvia_product_codes_by_brand", fake)
    codes = service._general_franchise_codes("존재하지않는브랜드XYZ", {"XCODE"})
    assert codes == {"XCODE"}  # no ml mapping -> selected-only (legacy)
    assert calls["n"] == 0  # no extra IQVIA lookup for unmapped brand


def test_general_franchise_qualifying_matches_strategic_membership(monkeypatch) -> None:
    # View-independence: the general franchise brand membership equals the strategic_ml
    # is_jw membership for the same ml_id (리바로 -> {리바로, 리바로젯}).
    monkeypatch.setattr(
        service,
        "iqvia_product_codes_by_brand",
        lambda names: {n: (n.upper(),) for n in names},
    )
    codes = service._general_franchise_codes("리바로", {"리바로".upper()})
    # both franchise members contribute; non-ml_006 siblings (페노/하이/브이) absent
    assert "리바로".upper() in codes and "리바로젯".upper() in codes
    assert "리바로페노".upper() not in codes and "리바로하이".upper() not in codes
