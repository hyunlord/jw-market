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
            "market_id": "C10A1",
            "selected_brand": "LIVALO",
            "entity_level": "company",
            "csd_channel": "GH+SHPPI",
            "top5_basis": "activity_count",
        }
    )

    assert payload is not None
    assert payload["entity_level"] == "company"
    assert payload["channel"] == "GH+SHPPI"
    assert payload["period"]["quarters"] == ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]
    assert any(params == ("LIVALO Market", "GH+SHPPI") for params in captured_params)
    selected = payload["entities"][0]
    assert selected["key"] == "JW"
    assert selected["is_selected"] is True
    assert selected["activity"]["absolute"][-1] == {"period": "2025-Q4", "value": 130.0}
    assert selected["activity"]["share_pct"][-1] == {"period": "2025-Q4", "value": 38.23529411764706}
    assert selected["activity"]["rank"][-1] == {"period": "2025-Q4", "value": 2}


def test_activity_series_rejects_unknown_channel() -> None:
    try:
        service.get_csd_activity_series(
            {"view": "general", "market_id": "C10A1", "selected_brand": "LIVALO", "csd_channel": "BAD"}
        )
    except service.CsdActivitySeriesInputError as exc:
        assert "unsupported csd_channel" in str(exc)
    else:
        raise AssertionError("expected CsdActivitySeriesInputError")


def test_activity_series_top5_basis_changes_competitor_order(monkeypatch) -> None:
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

    csd_market = service.get_csd_activity_series(_request(top5_basis="csd_market", period={"start": "2025-Q4", "end": "2025-Q4"}))
    activity_count = service.get_csd_activity_series(_request(top5_basis="activity_count", period={"start": "2025-Q4", "end": "2025-Q4"}))

    assert csd_market is not None
    assert activity_count is not None
    assert [entity["key"] for entity in csd_market["entities"][:3]] == ["LIVALO", "C", "A"]
    assert [entity["key"] for entity in activity_count["entities"][:3]] == ["LIVALO", "A", "LIVALOZET"]


def test_activity_series_ignores_audit_code_for_csd_based_top5(monkeypatch) -> None:
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
            "market_id": "C10A1",
            "selected_brand": "LIVALO",
            "filters": {"atc4": ["C10A1"], "channel_axis": {"iqvia": {"audit_code": ["BAD"]}}},
            "top5_basis": "activity_count",
        }
    )

    assert payload is not None
    assert captured["filter_payload"] == {"atc4": ["C10A1"]}


def test_requested_quarters_defaults_to_one_year_and_clamps_to_three_years() -> None:
    all_quarters = tuple(f"{year}-Q{quarter}" for year in (2022, 2023, 2024, 2025) for quarter in range(1, 5))

    defaulted = service._requested_quarters(all_quarters, {})
    clamped = service._requested_quarters(all_quarters, {"start": "2022Q1", "end": "2025Q4"})

    assert defaulted == ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4")
    assert len(clamped) == 12
    assert clamped[0] == "2023-Q1"
    assert clamped[-1] == "2025-Q4"


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
            "filters": {"channel_axis": {"iqvia": {"audit_code": ["KHPA"]}}},
            "entity_level": "brand",
            "csd_channel": "CPPI",
            "top5_basis": "csd_market",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"data": expected}
    assert captured["payload"]["csd_channel"] == "CPPI"
    assert captured["payload"]["filters"] == {"channel_axis": {"iqvia": {"audit_code": ["KHPA"]}}}


def test_csd_activity_series_service_uses_select_only_sql() -> None:
    source = Path("pipeline/scripts/api/brand_activity_csd_activity_series.py").read_text()
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ", "TRUNCATE ", "REPLACE ")

    assert not any(token in source.upper() for token in forbidden)


def _request(*, top5_basis: str, period: dict[str, str]) -> dict[str, Any]:
    return {
        "view": "general",
        "market_id": "C10A1",
        "selected_brand": "LIVALO",
        "entity_level": "brand",
        "csd_channel": "TOTAL",
        "top5_basis": top5_basis,
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
