from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_csd_activity_series as service
from pipeline.scripts.api import brand_activity_csd_timeseries as timeseries_service
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig
from pipeline.scripts.api.routes import brand_activity


@pytest.fixture(autouse=True)
def iqvia_product_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    def from_brand_meta(brands: dict[str, str]) -> dict[str, tuple[str, ...]]:
        brand_meta = _brand_set().brand_meta
        return {
            brand_key: tuple(brand_meta[brand_key].product_codes)
            for brand_key in brands
        }

    monkeypatch.setattr(timeseries_service, "iqvia_product_codes_by_brand", from_brand_meta)


def test_activity_series_company_axis_uses_channel_and_ranks_by_quarter(monkeypatch) -> None:
    captured_params: list[tuple[Any, ...] | None] = []

    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        captured_params.append(params)
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())

    payload = service.get_csd_activity_series(
        {
            "view": "general",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"]},
            "entity_level": "company",
            "csd_channel": "GH+SHPPI",
        }
    )

    assert payload is not None
    assert payload["entity_level"] == "company"
    assert payload["channel"] == "GH+SHPPI"
    assert payload["period"]["quarters"] == ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]
    assert payload["period"]["months"] == [
        "2025-01",
        "2025-02",
        "2025-03",
        "2025-04",
        "2025-05",
        "2025-06",
        "2025-07",
        "2025-08",
        "2025-09",
        "2025-10",
        "2025-11",
        "2025-12",
    ]
    assert any(
        params == ("LIVALO Market", "GH+SHPPI", "2025-01", "2025-12")
        for params in captured_params
    )
    selected = payload["entities"][0]
    assert selected["key"] == "JW"
    assert selected["is_selected"] is True
    assert {"period": "2025-10", "value": 80.0} in selected["activity"]["absolute"]
    assert {"period": "2025-10", "value": 23.52941176470588} in selected["activity"]["share_pct"]
    assert {"period": "2025-10", "value": 2} in selected["activity"]["rank"]
    assert {"period": "2025-11", "value": 0.0} in selected["activity"]["absolute"]


def test_activity_query_is_bounded_to_requested_months(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, tuple[Any, ...] | None]] = []

    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        captured.append((sql, params))
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())

    payload = service.get_csd_activity_series(
        _request(period={"start": "2025-Q2", "end": "2025-Q3"})
    )

    assert payload is not None
    activity_sql, activity_params = next(
        (sql, params)
        for sql, params in captured
        if "GROUP BY period_ym, master_product, representing_company" in sql
    )
    assert "period_ym BETWEEN %s AND %s" in activity_sql
    assert activity_params == ("LIVALO Market", "TOTAL", "2025-04", "2025-09")


def test_company_axis_aggregates_brand_series_with_mart_company_labels(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            source_companies = {
                "LIVALO": "JW PHARMACEUTICAL",
                "A": "VIATRIS",
                "B": "ASTRAZENECA KOREA",
                "C": "YUHAN CO.",
            }
            return [
                {
                    **row,
                    "representing_company": source_companies.get(
                        str(row["master_product"]), str(row["representing_company"])
                    ),
                }
                for row in _activity_rows()
            ]
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set_with_korean_companies())

    brand_payload = service.get_csd_activity_series(
        {**_request(period={"start": "2025-Q4", "end": "2025-Q4"}), "entity_level": "brand"}
    )
    company_payload = service.get_csd_activity_series(
        {**_request(period={"start": "2025-Q4", "end": "2025-Q4"}), "entity_level": "company"}
    )

    assert brand_payload is not None
    assert company_payload is not None
    assert [entity["key"] for entity in company_payload["entities"]] == ["JW중외제약", "비아트리스", "아스트라제네카", "유한양행"]
    for month in company_payload["period"]["months"]:
        brand_total = sum(_point(entity, "absolute", month) for entity in brand_payload["entities"])
        company_total = sum(_point(entity, "absolute", month) for entity in company_payload["entities"])
        assert company_total == brand_total
    assert any(_point(entity, "absolute", "2025-10") > 0 for entity in company_payload["entities"])
    assert sum(_point(entity, "share_pct", "2025-10") for entity in company_payload["entities"]) > 0
    assert all(_point(entity, "rank", "2025-10") is not None for entity in company_payload["entities"])


def test_activity_series_uses_resolved_brand_identity_for_entity_axes(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())

    brand_payload = service.get_csd_activity_series(
        {**_request(period={"start": "2025-Q4", "end": "2025-Q4"}), "selected_brand": "LIVALO DISPLAY"}
    )
    company_payload = service.get_csd_activity_series(
        {
            **_request(period={"start": "2025-Q4", "end": "2025-Q4"}),
            "selected_brand": "LIVALO DISPLAY",
            "entity_level": "company",
        }
    )

    assert brand_payload is not None
    assert company_payload is not None
    assert brand_payload["entities"][0]["key"] == "LIVALO"
    assert brand_payload["entities"][0]["is_selected"] is True
    assert company_payload["entities"][0]["key"] == "JW"
    assert company_payload["entities"][0]["is_selected"] is True


def test_company_axis_keeps_missing_mart_company_in_visible_unclassified_bucket(monkeypatch) -> None:
    base = _brand_set_with_korean_companies()
    brand_set = BrandSetResolution(
        view_name=base.view_name,
        market_id=base.market_id,
        selected_brand=base.selected_brand,
        view=base.view,
        market_row=base.market_row,
        brand_rows=tuple(
            {**row, "by_dimension": {}}
            if row["brand_key"] == "A"
            else row
            for row in base.brand_rows
        ),
        brand_meta=base.brand_meta,
        choices=base.choices,
        candidates=base.candidates,
        ranking_quarter=base.ranking_quarter,
        applied_filter=base.applied_filter,
    )
    activity = service._activity_rows(
        [
            {"period_ym": "2025-01", "master_product": "A", "representing_company": "UNKNOWN", "value": 10.0},
            {"period_ym": "2025-01", "master_product": "B", "representing_company": "KNOWN", "value": 20.0},
        ],
        ("2025-01",),
        ("2025-01",),
    )

    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: brand_set)
    monkeypatch.setattr(
        service,
        "resolve_csd_markets",
        lambda **_kwargs: (service.CsdCrosswalk("LIVALO Market", "LIVALO Market", ("A", "B"), 2),),
    )
    monkeypatch.setattr(
        service,
        "_fetch_activity_rows",
        lambda *_args: [
            {"period_ym": "2025-01", "master_product": "A", "representing_company": "UNKNOWN", "value": 10.0},
            {"period_ym": "2025-01", "master_product": "B", "representing_company": "KNOWN", "value": 20.0},
        ],
    )
    monkeypatch.setattr(
        db,
        "fetch_all",
        lambda sql, _params=None: [{"period_ym": month} for month in ("2025-01", "2025-02", "2025-03")]
        if "SELECT DISTINCT period_ym" in sql
        else [],
    )

    values = service._company_activity_by_key(brand_set, activity)
    payload = service.get_csd_activity_series(
        {"view": "general", "selected_brand": "LIVALO", "filters": {"atc4": ["C10A1"]}, "entity_level": "company"}
    )

    assert values["미분류"] == {"2025-01": 10.0}
    assert payload is not None
    unclassified = next(entity for entity in payload["entities"] if entity["key"] == "미분류")
    assert unclassified["activity"]["absolute"][0] == {"period": "2025-01", "value": 10.0}
    assert all(point["value"] == 0.0 for point in unclassified["activity"]["absolute"][1:])


def test_activity_series_rejects_unknown_channel() -> None:
    try:
        service.get_csd_activity_series(
            {"view": "general", "selected_brand": "LIVALO", "filters": {"atc4": ["C10A1"]}, "csd_channel": "BAD"}
        )
    except service.CsdActivitySeriesInputError as exc:
        assert "unsupported csd_channel" in str(exc)
    else:
        raise AssertionError("expected CsdActivitySeriesInputError")


def test_activity_series_ignores_stale_top5_basis_and_uses_iqvia_order(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())

    defaulted = service.get_csd_activity_series(_request(period={"start": "2025-Q4", "end": "2025-Q4"}))
    stale_field = service.get_csd_activity_series(
        {**_request(period={"start": "2025-Q4", "end": "2025-Q4"}), "top5_basis": "activity_count"}
    )

    assert defaulted is not None
    assert stale_field is not None
    assert [entity["key"] for entity in defaulted["entities"][:3]] == ["LIVALO", "A", "B"]
    assert [entity["key"] for entity in stale_field["entities"][:3]] == ["LIVALO", "A", "B"]
    assert "top5_basis" not in defaulted["applied"]


def test_activity_series_passes_audit_code_to_iqvia_brand_resolver(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    def fake_resolve_brand_set(**kwargs: Any) -> BrandSetResolution:
        captured["filter_payload"] = kwargs["filter_payload"]
        return _brand_set()

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", fake_resolve_brand_set)

    payload = service.get_csd_activity_series(
        {
            "view": "general",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"], "analysis_level": {"iqvia": {"audit_code": ["BAD"]}}},
            "top5_basis": "activity_count",
        }
    )

    assert payload is not None
    assert captured["filter_payload"] == {"atc4": ["C10A1"], "analysis_level": {"iqvia": {"audit_code": ["BAD"]}}}


def test_requested_quarters_defaults_to_one_year_and_clamps_to_three_years() -> None:
    all_quarters = tuple(f"{year}-Q{quarter}" for year in (2022, 2023, 2024, 2025) for quarter in range(1, 5))

    defaulted = service._requested_quarters(all_quarters, {})
    clamped = service._requested_quarters(all_quarters, {"start": "2022Q1", "end": "2025Q4"})

    assert defaulted == ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4")
    assert len(clamped) == 12
    assert clamped[0] == "2023-Q1"
    assert clamped[-1] == "2025-Q4"


def test_activity_rows_preserve_months_inside_requested_quarter() -> None:
    rows = [
        {"period_ym": "2025-01", "master_product": "LIVALO", "representing_company": "JW", "value": 10.0},
        {"period_ym": "2025-02", "master_product": "LIVALO", "representing_company": "JW", "value": 20.0},
        {"period_ym": "2025-04", "master_product": "LIVALO", "representing_company": "JW", "value": 40.0},
    ]

    activity = service._activity_rows(rows, ("2025-01", "2025-02", "2025-03"), ("2025-01", "2025-02", "2025-03", "2025-04"))

    assert activity.months == ("2025-01", "2025-02", "2025-03")
    assert activity.totals == {"2025-01": 10.0, "2025-02": 20.0, "2025-03": 0.0}
    assert activity.by_product["LIVALO"] == {"2025-01": 10.0, "2025-02": 20.0}
    assert activity.by_company["JW"] == {"2025-01": 10.0, "2025-02": 20.0}


def test_csd_activity_series_route_wraps_success_envelope(monkeypatch) -> None:
    expected = {"scope": {"view": "general"}, "entities": []}
    captured: dict[str, Any] = {}

    def fake_get_csd_activity_series(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return expected

    monkeypatch.setattr(brand_activity, "get_csd_activity_series", fake_get_csd_activity_series)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-activity-series",
        json={
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"], "analysis_level": {"iqvia": {"audit_code": ["KHPA"]}}},
            "entity_level": "brand",
            "csd_channel": "CPPI",
            "top5_basis": "csd_market",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"data": expected}
    assert captured["payload"]["csd_channel"] == "CPPI"
    assert "market_id" not in captured["payload"]
    assert "top5_basis" not in captured["payload"]
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filters"]["analysis_level"] == {"iqvia": {"audit_code": ["KHPA"]}}
    assert captured["payload"]["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_brand_activity_openapi_hides_removed_request_fields() -> None:
    app = FastAPI()
    app.include_router(brand_activity.router)

    schemas = app.openapi()["components"]["schemas"]
    request_names = (
        "CsdTimeseriesRequest",
        "BrandActivityTopicsRequest",
        "BrandActivityInterestRxRequest",
        "CsdActivitySeriesRequest",
    )
    for name in request_names[:3]:
        assert schemas[name]["properties"]["market_id"]["anyOf"][0]["type"] == "string"
    assert "market_id" not in schemas[request_names[3]]["properties"]
    assert "top5_basis" not in schemas["CsdActivitySeriesRequest"]["properties"]


def test_brand_activity_request_models_ignore_stale_market_id_and_top5_basis() -> None:
    common = {
        "view": "general",
        "market_id": "STALE",
        "selected_brand": "LIVALO",
        "filters": {"atc4": ["C10A1"]},
        "top5_basis": "activity_count",
    }
    models = (
        brand_activity.CsdTimeseriesRequest,
        brand_activity.BrandActivityTopicsRequest,
        brand_activity.BrandActivityInterestRxRequest,
        brand_activity.CsdActivitySeriesRequest,
    )

    for model in models[:3]:
        dumped = model(**common).model_dump()
        assert dumped["market_id"] == "STALE"
        assert "top5_basis" not in dumped
    dumped = models[3](**common).model_dump()
    assert "market_id" not in dumped
    assert "top5_basis" not in dumped


def test_csd_activity_series_service_uses_select_only_sql() -> None:
    source = Path("pipeline/scripts/api/brand_activity_csd_activity_series.py").read_text()
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ", "TRUNCATE ", "REPLACE ")

    assert not any(token in source.upper() for token in forbidden)


def test_activity_series_parse_accepts_general_market_scope_without_atc4() -> None:
    parsed = service.parse_activity_request(
        {
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        }
    )

    assert parsed.market_id is None
    assert parsed.filter_payload["market_scope"] == {"option_id": "group:livalo_family", "member": "리바로"}


def test_activity_series_parse_accepts_strategic_cd() -> None:
    parsed = service.parse_activity_request(
        {
            "view": "strategic_cd",
            "selected_brand": "가드렛",
            "filters": {"market_scope": {"market_id": "cd_003"}},
        }
    )

    assert parsed.view == "strategic_cd"
    assert parsed.market_id is None


def test_activity_series_selected_brand_list_matches_string_response(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": period} for period in _months()]
        if "GROUP BY market, master_product" in sql:
            return [{"market": "LIVALO Market", "master_product": product} for product in ("LIVALO", "A", "B", "C")]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            return _activity_rows()
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    string_payload = _request(period={"start": "2025-Q1", "end": "2025-Q4"})
    list_payload = {**string_payload, "selected_brand": [" ", "LIVALO", "OTHER"]}

    string_response = service.get_csd_activity_series(string_payload)
    list_response = service.get_csd_activity_series(list_payload)

    assert list_response == string_response
    assert list_response is not None
    assert list_response["scope"]["selected_brand"] == "LIVALO"


def test_csd_activity_series_request_accepts_string_and_list_selected_brand() -> None:
    common = {"view": "general", "filters": {"atc4": ["C10A1"]}}

    string_request = brand_activity.CsdActivitySeriesRequest(**common, selected_brand="LIVALO")
    list_request = brand_activity.CsdActivitySeriesRequest(**common, selected_brand=["LIVALO"])

    assert string_request.selected_brand == "LIVALO"
    assert list_request.selected_brand == ["LIVALO"]


def test_activity_request_preserves_optional_csd_market_filter() -> None:
    parsed = service.parse_activity_request(
        {
            "view": "general",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"]},
            "csd_market": "LIVALO FENO",
        }
    )

    assert parsed.csd_market == "LIVALO FENO"


def test_activity_series_brand_join_uses_iqvia_codes_instead_of_ubist_meta() -> None:
    brand_set = _brand_set()
    brand_set.brand_meta["LIVALO"] = BrandMeta(
        "LIVALO",
        "LIVALO",
        ("UBIST-LIVALO",),
        True,
    )
    activity = service.ActivityRows(
        months=("2025-01",),
        all_months=("2025-01",),
        totals={"2025-01": 17.0},
        by_product={"LIVALO": {"2025-01": 17.0}},
        by_company={"JW": {"2025-01": 17.0}},
        observed_months=("2025-01",),
    )

    values = service._brand_activity_by_key(
        brand_set,
        activity,
        {"LIVALO": frozenset({"LIVALO"})},
    )

    assert values["LIVALO"] == {"2025-01": 17.0}


def test_activity_series_exposes_multimarket_union_and_filter_without_cross_call_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    months = ("2024-01", "2024-02", "2024-03", "2025-01", "2025-02", "2025-03")

    def fake_fetch_all(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT DISTINCT period_ym" in sql:
            return [{"period_ym": month} for month in months]
        if "GROUP BY market, master_product" in sql:
            return [
                {"market": "LIVALO Market", "master_product": "LIVALO"},
                {"market": "LIVALO Market", "master_product": "RIVAL"},
                {"market": "LIVALO FENO Market", "master_product": "LIVALO FENO"},
                {"market": "COMPETITOR ONLY Market", "master_product": "RIVAL"},
            ]
        if "GROUP BY period_ym, master_product, representing_company" in sql:
            market = str(params[0]) if params else ""
            if market == "LIVALO Market":
                return [
                    {"period_ym": month, "master_product": "LIVALO", "representing_company": "JW", "value": 10.0}
                    for month in months
                ]
            if market == "LIVALO FENO Market":
                return [
                    {"period_ym": month, "master_product": "LIVALO FENO", "representing_company": "JW", "value": 3.0}
                    for month in months[-3:]
                ]
        raise AssertionError(f"unexpected sql: {sql}")

    codes = service.CsdProductCodes(
        selected=frozenset({"LIVALO", "LIVALO FENO"}),
        candidates=frozenset({"LIVALO", "LIVALO FENO", "RIVAL"}),
        by_brand={"LIVALO": frozenset({"LIVALO", "LIVALO FENO"})},
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(service, "_iqvia_csd_product_codes", lambda *_args, **_kwargs: codes)
    base_request = _request(period={"start": "2024-Q1", "end": "2025-Q1"})

    filtered_first = service.get_csd_activity_series({**base_request, "csd_market": "LIVALO FENO"})
    unfiltered = service.get_csd_activity_series(base_request)
    filtered_again = service.get_csd_activity_series({**base_request, "csd_market": "LIVALO FENO"})

    assert filtered_first == filtered_again
    assert filtered_first is not None
    assert filtered_first["scope"]["csd_markets"] == ["LIVALO", "LIVALO FENO"]
    assert filtered_first["scope"]["csd_market"] == "LIVALO FENO"
    assert _point(filtered_first["entities"][0], "absolute", "2025-01") == 3.0
    assert _point(filtered_first["entities"][0], "share_pct", "2025-01") == 100.0
    assert unfiltered is not None
    assert unfiltered["scope"]["csd_markets"] == ["LIVALO", "LIVALO FENO"]
    assert "COMPETITOR ONLY" not in unfiltered["series_by_csd_market"]
    assert unfiltered["scope"]["csd_market"] is None
    assert unfiltered["aggregate"]["available"] == {
        "LIVALO": {"start": "2024-01", "end": "2025-03"},
        "LIVALO FENO": {"start": "2025-01", "end": "2025-03"},
    }
    assert unfiltered["aggregate"]["series"]["market_totals"]["2024-01"] == 10.0
    assert unfiltered["aggregate"]["series"]["market_totals"]["2025-01"] == 13.0
    assert _point(unfiltered["entities"][0], "absolute", "2025-01") == 13.0
    assert _point(unfiltered["entities"][0], "share_pct", "2025-01") == 100.0
    _assert_multimarket_union_contract(unfiltered)
    assert unfiltered["aggregate"]["contributing_markets_by_period"]["2024-01"] == ["LIVALO"]
    assert unfiltered["aggregate"]["contributing_markets_by_period"]["2025-01"] == ["LIVALO", "LIVALO FENO"]
    assert "2024-01" not in unfiltered["series_by_csd_market"]["LIVALO FENO"]["market_totals"]

    with pytest.raises(service.CsdMarketFilterError):
        service.get_csd_activity_series({**base_request, "csd_market": "COMPETITOR ONLY"})


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("entity_below_market", "aggregate entity below selected market"),
        ("total_sigma", "aggregate market total sigma mismatch"),
        ("member_union", "aggregate member union mismatch"),
    ),
)
def test_multimarket_union_gate_rejects_required_failure_injections(
    mutation: str,
    message: str,
) -> None:
    injected = deepcopy(_multimarket_union_gate_fixture())

    if mutation == "entity_below_market":
        injected["aggregate"]["series"]["by_entity"]["LIVALO"]["2025-01"] = 9.0
    elif mutation == "total_sigma":
        injected["aggregate"]["series"]["market_totals"]["2025-01"] = 14.0
    else:
        del injected["aggregate"]["series"]["by_entity"]["LIVALO"]

    with pytest.raises(AssertionError, match=message):
        _assert_multimarket_union_contract(injected)


def test_activity_series_rejects_csd_market_outside_resolved_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "fetch_all", lambda sql, _params=None: [{"period_ym": month} for month in _months()] if "SELECT DISTINCT period_ym" in sql else [])
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(
        service,
        "resolve_csd_markets",
        lambda **_kwargs: (service.CsdCrosswalk("LIVALO Market", "LIVALO", ("LIVALO",), 1),),
    )

    with pytest.raises(service.CsdMarketFilterError) as exc_info:
        service.get_csd_activity_series({**_request(period={}), "csd_market": "UNKNOWN"})

    assert exc_info.value.available == ("LIVALO",)


def test_activity_series_route_returns_422_for_unknown_csd_market(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_payload: dict[str, Any]) -> None:
        raise service.CsdMarketFilterError("UNKNOWN", available=("LIVALO",))

    monkeypatch.setattr(brand_activity, "get_csd_activity_series", reject)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-activity-series",
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


def test_activity_series_selected_brand_list_keeps_required_validation() -> None:
    for selected_brand in ([], ["", "  "]):
        try:
            service.parse_activity_request(
                {
                    "view": "general",
                    "selected_brand": selected_brand,
                    "filters": {"atc4": ["C10A1"]},
                }
            )
        except service.CsdActivitySeriesInputError as exc:
            assert str(exc) == "filters.atc4 and selected_brand are required"
        else:
            raise AssertionError("expected CsdActivitySeriesInputError")


def _request(*, period: dict[str, str]) -> dict[str, Any]:
    return {
        "view": "general",
        "selected_brand": "LIVALO",
        "filters": {"atc4": ["C10A1"]},
        "entity_level": "brand",
        "csd_channel": "TOTAL",
        "period": period,
    }


def test_activity_series_applies_csd_franchise_gate(monkeypatch) -> None:
    # ★ core of the closing round: csd-activity-series (the endpoint the portal actually calls)
    # now feeds the same ml_id franchise qualification into resolve_csd_markets as csd-timeseries,
    # so a same-ml_id JW sibling (LIVALOZET) qualifies and candidate is widened for discovery.
    codes = {"리바로": ("LIVALO",), "리바로젯": ("LIVALOZET",), "크레스토": ("CRESTOR",)}
    monkeypatch.setattr(
        timeseries_service, "iqvia_product_codes_by_brand",
        lambda names: {k: codes[k] for k in names if k in codes},
    )
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_set = BrandSetResolution(
        view_name="general", market_id="C10A1", selected_brand="리바로", view=view,
        market_row={"atc4_code": "C10A1", "atc4_desc": "STATINS"},
        brand_rows=(
            {"brand_key": "리바로", "brand_name": "리바로", "by_dimension": {"company": "JW"}},
            {"brand_key": "크레스토", "brand_name": "크레스토", "by_dimension": {"company": "AstraZeneca"}},
        ),
        brand_meta={
            "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True),
            "크레스토": BrandMeta("크레스토", "크레스토", ("CRESTOR",), False),
        },
        choices=(BrandChoice("리바로", "리바로", 1, True), BrandChoice("크레스토", "크레스토", 2, False)),
        candidates=(), ranking_quarter="2025-Q4", applied_filter={"atc4": ["C10A1"]},
    )
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: brand_set)
    captured: dict[str, object] = {}

    def fake_resolve_csd_markets(**kwargs):
        captured.update(kwargs)
        return (service.CsdCrosswalk("LIVALO Market", "LIVALO Market", ("LIVALO",), 1),)

    monkeypatch.setattr(service, "resolve_csd_markets", fake_resolve_csd_markets)
    monkeypatch.setattr(service, "_fetch_activity_rows", lambda *_a: [])
    monkeypatch.setattr(
        db, "fetch_all",
        lambda sql, _params=None: [{"period_ym": m} for m in ("2025-01", "2025-02", "2025-03")]
        if "SELECT DISTINCT period_ym" in sql else [],
    )

    payload = service.get_csd_activity_series(
        {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}, "entity_level": "brand"}
    )
    assert payload is not None
    assert captured["qualifying_product_codes"] == {"LIVALO", "LIVALOZET"}
    assert "LIVALOZET" in captured["candidate_product_codes"]  # candidate widened for sheet discovery
    assert captured["selected_product_codes"] == {"LIVALO"}


def _brand_set() -> BrandSetResolution:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_meta = {
        "LIVALO": BrandMeta("LIVALO", "LIVALO", ("LIVALO",), True),
        "A": BrandMeta("A", "A", ("A",), False),
        "B": BrandMeta("B", "B", ("B",), False),
        "C": BrandMeta("C", "C", ("C",), False),
    }
    return BrandSetResolution(
        view_name="general",
        market_id="C10A1",
        selected_brand="LIVALO",
        view=view,
        market_row={"atc4_code": "C10A1", "atc4_desc": "STATINS"},
        brand_rows=(
            {"brand_key": "LIVALO", "brand_name": "LIVALO", "by_dimension": {"company": "JW"}},
            {"brand_key": "A", "brand_name": "A", "by_dimension": {"company": "Alpha"}},
            {"brand_key": "B", "brand_name": "B", "by_dimension": {"company": "Beta"}},
            {"brand_key": "C", "brand_name": "C", "by_dimension": {"company": "Gamma"}},
        ),
        brand_meta=brand_meta,
        choices=(
            BrandChoice("LIVALO", "LIVALO", 2, True),
            BrandChoice("A", "A", 1, False),
            BrandChoice("B", "B", 3, False),
            BrandChoice("C", "C", 4, False),
        ),
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={"atc4": ["C10A1"]},
    )


def _brand_set_with_korean_companies() -> BrandSetResolution:
    base = _brand_set()
    companies = {
        "LIVALO": "JW중외제약",
        "A": "비아트리스",
        "B": "아스트라제네카",
        "C": "유한양행",
    }
    return BrandSetResolution(
        view_name=base.view_name,
        market_id=base.market_id,
        selected_brand=base.selected_brand,
        view=base.view,
        market_row=base.market_row,
        brand_rows=tuple(
            {
                "brand_key": row["brand_key"],
                "brand_name": row["brand_name"],
                "by_dimension": {"company": companies[str(row["brand_key"])]},
            }
            for row in base.brand_rows
        ),
        brand_meta=base.brand_meta,
        choices=base.choices,
        candidates=base.candidates,
        ranking_quarter=base.ranking_quarter,
        applied_filter=base.applied_filter,
    )


def _assert_multimarket_union_contract(payload: dict[str, Any], tolerance: float = 1e-9) -> None:
    by_market = payload["series_by_csd_market"]
    aggregate = payload["aggregate"]["series"]
    aggregate_totals = aggregate["market_totals"]
    aggregate_entities = aggregate["by_entity"]
    periods = set(aggregate_totals)

    for period in periods:
        market_sum = sum(market["market_totals"].get(period, 0.0) for market in by_market.values())
        assert abs(aggregate_totals[period] - market_sum) <= tolerance, "aggregate market total sigma mismatch"

    market_members = {
        entity
        for market in by_market.values()
        for entity, values in market["by_entity"].items()
        if any(abs(value) > tolerance for value in values.values())
    }
    aggregate_members = {
        entity
        for entity, values in aggregate_entities.items()
        if any(abs(value) > tolerance for value in values.values())
    }
    assert aggregate_members == market_members, "aggregate member union mismatch"

    for entity in market_members:
        for period in periods:
            aggregate_value = aggregate_entities.get(entity, {}).get(period, 0.0)
            for market in by_market.values():
                assert (
                    aggregate_value + tolerance >= market["by_entity"].get(entity, {}).get(period, 0.0)
                ), "aggregate entity below selected market"

    entities = {entity["key"]: entity for entity in payload["entities"]}
    for entity, response in entities.items():
        for period in periods:
            value = _point(response, "absolute", period)
            expected_value = aggregate_entities.get(entity, {}).get(period, 0.0)
            assert value == pytest.approx(expected_value, abs=tolerance)
            expected_share = 100.0 * expected_value / aggregate_totals[period] if aggregate_totals[period] else None
            assert _point(response, "share_pct", period) == pytest.approx(expected_share, abs=tolerance)


def _multimarket_union_gate_fixture() -> dict[str, Any]:
    return {
        "entities": [
            {
                "key": "LIVALO",
                "activity": {
                    "absolute": [{"period": "2025-01", "value": 13.0}],
                    "share_pct": [{"period": "2025-01", "value": 100.0}],
                },
            }
        ],
        "series_by_csd_market": {
            "LIVALO": {
                "market_totals": {"2025-01": 10.0},
                "by_entity": {"LIVALO": {"2025-01": 10.0}},
            },
            "LIVALO FENO": {
                "market_totals": {"2025-01": 3.0},
                "by_entity": {"LIVALO": {"2025-01": 3.0}},
            },
        },
        "aggregate": {
            "series": {
                "market_totals": {"2025-01": 13.0},
                "by_entity": {"LIVALO": {"2025-01": 13.0}},
            }
        },
    }


def _point(entity: dict[str, Any], metric: str, period: str) -> float | int | None:
    return next(item["value"] for item in entity["activity"][metric] if item["period"] == period)


def _months() -> list[str]:
    return [f"{year}-{month:02d}" for year in (2024, 2025) for month in range(1, 13)]


def _activity_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    values = {
        "LIVALO": ("JW", [10, 20, 30, 40, 50, 60, 70, 80]),
        "LIVALOZET": ("JW", [5, 10, 15, 20, 25, 30, 40, 50]),
        "A": ("Alpha", [40, 50, 60, 70, 80, 90, 100, 160]),
        "B": ("Beta", [30, 30, 30, 30, 30, 30, 30, 30]),
        "C": ("Gamma", [200, 200, 200, 200, 20, 20, 20, 20]),
    }
    quarters = [("2024", 1), ("2024", 4), ("2024", 7), ("2024", 10), ("2025", 1), ("2025", 4), ("2025", 7), ("2025", 10)]
    for product, (company, per_quarter) in values.items():
        for (year, start_month), total in zip(quarters, per_quarter, strict=True):
            rows.append(
                {
                    "period_ym": f"{year}-{start_month:02d}",
                    "master_product": product,
                    "representing_company": company,
                    "value": float(total),
                }
            )
    return rows
