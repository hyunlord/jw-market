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


def test_strategic_market_id_is_preserved_for_context_disambiguation() -> None:
    payload = brand_activity.BrandActivityTopicsRequest.model_validate(
        {
            "view": "strategic_ml",
            "market_id": "ml_006",
            "selected_brand": "리바로",
            "filters": {"atc": {"atc4": ["C10A1"]}},
        }
    )

    service_payload, normalized = brand_activity._portal_service_request(payload)

    assert service_payload["market_id"] == "ml_006"
    assert normalized is True


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_portal_strategic_ml_payload_reaches_all_three_services(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    captured: dict[str, object] = {}

    def getter(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"scope": {"view": "strategic_ml", "market_id": "ml_006"}, "brands": [{"brand_key": "리바로"}]}

    response = _client(monkeypatch, getter_name, getter).post(
        f"/api/brand-activity/{route}",
        json={
            "view": "strategic_ml",
            "selected_brand": "리바로",
            "filters": {
                "atc": {"atc4": ["C10A1"]},
                "analysis_level": {"iqvia": {"audit_code": []}},
                "channel": {"visit_location": [], "specialty": []},
            },
        },
    )

    assert response.status_code == 200
    assert captured["view"] == "strategic_ml"
    assert captured["selected_brand"] == "리바로"
    assert captured["market_id"] is None


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_ambiguous_strategic_market_returns_shared_409(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError

    context_error = BrandSetInputError(
        "market_id is required because the brand belongs to multiple markets",
        status_code=409,
        error="ambiguous_market_context",
        available_contexts=({"view_kind": "strategic_ml", "market_id": "ml_003"}, {"view_kind": "strategic_ml", "market_id": "ml_006"}),
        requested={"brand": "리바로", "view": "strategic_ml", "market_id": None},
        hint="provide market_id from available_contexts",
    )
    wrapper_by_getter = {
        "get_topic_brand_payload": brand_activity.TopicRequestError,
        "get_csd_timeseries": brand_activity.CsdTimeseriesInputError,
        "get_interest_rx_matrix": brand_activity.InterestRxMatrixInputError,
    }

    def getter(_payload: dict[str, object]) -> dict[str, object]:
        raise wrapper_by_getter[getter_name]("service error") from context_error

    response = _client(monkeypatch, getter_name, getter).post(
        f"/api/brand-activity/{route}",
        json={"view": "strategic_ml", "selected_brand": "리바로", "filters": {}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["available_contexts"][0]["market_id"] == "ml_003"


@pytest.mark.parametrize(("route", "getter_name"), ROUTE_GETTERS)
def test_nonmember_strategic_brand_returns_structured_400(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    getter_name: str,
) -> None:
    from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError

    context_error = BrandSetInputError(
        "brand has no serving context for the requested view",
        status_code=400,
        error="brand_not_found",
        requested={"brand": "비소속", "view": "strategic_ml", "market_id": None},
        hint="verify strategic catalog membership",
    )
    wrapper_by_getter = {
        "get_topic_brand_payload": brand_activity.TopicRequestError,
        "get_csd_timeseries": brand_activity.CsdTimeseriesInputError,
        "get_interest_rx_matrix": brand_activity.InterestRxMatrixInputError,
    }

    def getter(_payload: dict[str, object]) -> dict[str, object]:
        raise wrapper_by_getter[getter_name]("service error") from context_error

    response = _client(monkeypatch, getter_name, getter).post(
        f"/api/brand-activity/{route}",
        json={"view": "strategic_ml", "selected_brand": "비소속", "filters": {}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error": "brand_not_found",
        "message": "brand has no serving context for the requested view",
        "requested": {"brand": "비소속", "view": "strategic_ml", "market_id": None},
        "hint": "verify strategic catalog membership",
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
