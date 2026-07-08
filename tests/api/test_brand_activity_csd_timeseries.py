from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_csd_timeseries as service
from pipeline.scripts.api import brand_activity_csd_shared as shared
from pipeline.scripts.api.routes import brand_activity


def test_period_ym_to_quarter_handles_boundaries() -> None:
    assert service.period_ym_to_quarter("2025-03") == "2025-Q1"
    assert service.period_ym_to_quarter("2025-04") == "2025-Q2"
    assert service.period_ym_to_quarter("2025-09") == "2025-Q3"
    assert service.period_ym_to_quarter("2025-10") == "2025-Q4"
    assert service.period_ym_to_quarter("2025-12") == "2025-Q4"


def test_full_csd_quarters_excludes_partial_edges() -> None:
    months = ["2023-05", "2023-06", "2023-07", "2023-08", "2023-09", "2025-10", "2025-11", "2025-12"]

    assert service.full_quarters_from_months(months) == ["2023-Q3", "2025-Q4"]


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
            "measures": ["activity", "unit", "counting_unit", "dosage_unit"],
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
    assert response.json() == {"data": expected}
    assert "market_id" not in captured
    assert captured["filters"]["atc4"] == ["C10A1"]
    assert captured["filters"]["analysis_level"] == {"iqvia": {"audit_code": ["KHPA"]}}
    assert captured["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_csd_timeseries_route_ignores_stale_market_id_input(monkeypatch) -> None:
    monkeypatch.setattr(brand_activity, "get_csd_timeseries", lambda _payload: None)
    app = FastAPI()
    app.include_router(brand_activity.router)

    response = TestClient(app).post(
        "/api/brand-activity/csd-timeseries",
        json={"view": "general", "market_id": "missing", "selected_brand": "리바로", "filter": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"data": None, "reason": "market_not_found"}


def test_csd_timeseries_service_uses_select_only_sql() -> None:
    source = Path("pipeline/scripts/api/brand_activity_csd_timeseries.py").read_text()
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ", "TRUNCATE ", "REPLACE ")

    assert not any(token in source.upper() for token in forbidden)
