from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_csd_activity_series as service
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig
from pipeline.scripts.api.routes import brand_activity


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
    assert any(params == ("LIVALO Market", "GH+SHPPI") for params in captured_params)
    selected = payload["entities"][0]
    assert selected["key"] == "JW"
    assert selected["is_selected"] is True
    assert {"period": "2025-10", "value": 80.0} in selected["activity"]["absolute"]
    assert {"period": "2025-10", "value": 23.52941176470588} in selected["activity"]["share_pct"]
    assert {"period": "2025-10", "value": 2} in selected["activity"]["rank"]
    assert {"period": "2025-11", "value": 0.0} in selected["activity"]["absolute"]


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
        "resolve_csd_market",
        lambda **_kwargs: service.CsdCrosswalk("LIVALO Market", "LIVALO Market", ("A", "B"), 2),
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
