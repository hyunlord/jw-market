from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.routes import brand_activity


RouteGetter = tuple[str, str]


ROUTE_GETTERS: tuple[RouteGetter, ...] = (
    ("topics", "get_topic_brand_payload"),
    ("csd-timeseries", "get_csd_timeseries"),
    ("interest-rx-matrix", "get_interest_rx_matrix"),
)


def _client(monkeypatch: pytest.MonkeyPatch, getter_name: str, getter: Callable[[dict[str, object]], dict[str, object] | None]) -> TestClient:
    monkeypatch.setattr(brand_activity, getter_name, getter)
    app = FastAPI()
    app.include_router(brand_activity.router)
    return TestClient(app)


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_nested_atc4_is_normalized_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    captured: dict[str, object] = {}

    def getter(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"scope": "fixture"}

    response = _client(monkeypatch, getter_name, getter).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc": {"atc4": ["C10A1"]}},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": {"scope": "fixture"},
        "meta": {"request_normalized": True},
    }
    captured_filters = captured["filters"]
    assert isinstance(captured_filters, dict)
    assert captured_filters["atc4"] == ["C10A1"]
    assert captured["view"] == "general"


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_matching_flat_and_nested_atc4_keeps_flat_value(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    captured: dict[str, object] = {}

    def getter(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"scope": "fixture"}

    response = _client(monkeypatch, getter_name, getter).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {
                "atc4": ["C10A1"],
                "atc": {"atc4": ["C10A1"]},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["meta"] == {"request_normalized": True}
    captured_filters = captured["filters"]
    assert isinstance(captured_filters, dict)
    assert captured_filters["atc4"] == ["C10A1"]


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_flat_atc4_response_bytes_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    response = _client(monkeypatch, getter_name, lambda _payload: {"scope": "fixture"}).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
        },
    )

    assert response.status_code == 200
    assert response.content == b'{"data":{"scope":"fixture"}}'


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_conflicting_flat_and_nested_atc4_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    response = _client(monkeypatch, getter_name, lambda _payload: {"scope": "fixture"}).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {
                "atc4": ["C10A1"],
                "atc": {"atc4": ["C10C0"]},
            },
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error": "conflicting_market_filter",
            "message": "filters.atc4 and filters.atc.atc4 must match when both are provided",
            "fields": ["filters.atc4", "filters.atc.atc4"],
        }
    }


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_market_not_found_uses_structured_404(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    response = _client(monkeypatch, getter_name, lambda _payload: None).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "general",
            "selected_brand": "없는브랜드",
            "filters": {"atc4": ["Z99Z9"]},
        },
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "error": "market_not_found",
            "message": "요청 필터로 시장을 식별할 수 없음",
            "requested": {
                "view": "general",
                "filters_received": {"atc4": ["Z99Z9"]},
            },
            "hint": "flat filters.atc4 or market_id expected",
        }
    }
