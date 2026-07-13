from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_csd_presence as service
from pipeline.scripts.api import brand_activity_csd_timeseries as timeseries
from pipeline.scripts.api.brand_activity_csd_shared import CsdTimeseriesNoMappingError
from pipeline.scripts.api.routes import brand_activity


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(brand_activity.router)
    return TestClient(app)


def test_presence_uses_the_same_product_overlap_as_csd_market_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_fetch_brand_rows",
        lambda _brands: [
            {
                "brand_key": "리바로",
                "brand_name": "리바로",
                "by_dimension": {"products": [{"product_code": "LIVALO"}]},
            }
        ],
    )
    monkeypatch.setattr(service, "_cached_csd_products", lambda: frozenset({"LIVALO"}))

    assert service.get_csd_presence("리바로") == {
        "brand": "리바로",
        "resolved": True,
        "csd_present": True,
        "reason": None,
    }
    monkeypatch.setattr(
        timeseries.db,
        "fetch_all",
        lambda _sql: [{"market": "Livalo Market", "master_product": "LIVALO"}],
    )
    assert timeseries.resolve_csd_market(
        selected_product_codes={"LIVALO"},
        candidate_product_codes={"LIVALO"},
    ).market == "Livalo Market"


def test_presence_maps_no_mapping_without_diverging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_fetch_brand_rows",
        lambda brands: [
            {
                "brand_key": brands[0],
                "brand_name": brands[0],
                "by_dimension": {"products": [{"product_code": "HEMLIBRA"}]},
            }
        ],
    )
    monkeypatch.setattr(service, "_cached_csd_products", lambda: frozenset({"LIVALO"}))
    assert service.get_csd_presence("헴리브라") == {
        "brand": "헴리브라",
        "resolved": True,
        "csd_present": False,
        "reason": "no_csd_mapping",
    }

    monkeypatch.setattr(
        timeseries.db,
        "fetch_all",
        lambda _sql: [{"market": "Livalo Market", "master_product": "LIVALO"}],
    )
    with pytest.raises(CsdTimeseriesNoMappingError):
        timeseries.resolve_csd_market(
            selected_product_codes={"HEMLIBRA"},
            candidate_product_codes={"HEMLIBRA"},
        )


def test_presence_returns_unresolved_for_unknown_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_fetch_brand_rows", lambda _brands: [])
    monkeypatch.setattr(service, "_cached_csd_products", lambda: frozenset())

    assert service.get_csd_presence("없는브랜드") == {
        "brand": "없는브랜드",
        "resolved": False,
        "csd_present": False,
        "reason": "brand_not_found",
    }


def test_brand_lookup_prefers_exact_then_compact_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fetch_all(_sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append(params)
        if len(calls) == 1:
            return [
                {"brand_key": "리바로", "brand_name": "리바로", "by_dimension": {}},
            ]
        return [
            {"brand_key": "리바로브이", "brand_name": "리바로 브이", "by_dimension": {}},
        ]

    monkeypatch.setattr(service.db, "fetch_all", fetch_all)

    rows = service._fetch_brand_rows(("리바로", "리바로브이"))

    assert [row["brand_key"] for row in rows] == ["리바로", "리바로브이"]
    assert len(calls) == 2
    assert calls[0] == ("iqvia_nsa", "리바로", "리바로브이", "리바로", "리바로브이")
    assert calls[1] == ("iqvia_nsa", "리바로브이", "리바로브이")


def test_presence_route_supports_single_and_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    def presence(brand: str) -> service.CsdPresence:
        return {
            "brand": brand,
            "resolved": brand != "없는브랜드",
            "csd_present": brand == "리바로",
            "reason": None if brand == "리바로" else "no_csd_mapping",
        }

    monkeypatch.setattr(brand_activity, "get_csd_presence", presence)
    monkeypatch.setattr(
        brand_activity,
        "get_csd_presences",
        lambda brands: [presence(brand) for brand in brands],
    )
    client = _client()

    single = client.get("/api/brand-activity/csd-presence", params={"brand": "리바로"})
    batch = client.get("/api/brand-activity/csd-presence", params={"brands": "리바로, 헴리브라"})

    assert single.status_code == 200
    assert single.json() == {
        "brand": "리바로",
        "resolved": True,
        "csd_present": True,
        "reason": None,
    }
    assert batch.status_code == 200
    assert [item["brand"] for item in batch.json()] == ["리바로", "헴리브라"]


def test_presence_route_rejects_invalid_query_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brand_activity, "get_csd_presence", lambda brand: {})
    client = _client()

    assert client.get("/api/brand-activity/csd-presence").status_code == 422
    assert client.get(
        "/api/brand-activity/csd-presence",
        params={"brand": "리바로", "brands": "리바로,헴리브라"},
    ).status_code == 422
    assert client.get(
        "/api/brand-activity/csd-presence",
        params={"brands": ",".join(f"브랜드{index}" for index in range(51))},
    ).status_code == 422


def test_presence_route_accepts_batch_of_fifty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_csd_presences",
        lambda brands: [
            {"brand": brand, "resolved": True, "csd_present": True, "reason": None}
            for brand in brands
        ],
    )
    brands = [f"브랜드{index}" for index in range(50)]

    response = _client().get(
        "/api/brand-activity/csd-presence",
        params={"brands": ",".join(brands)},
    )

    assert response.status_code == 200
    assert [item["brand"] for item in response.json()] == brands
