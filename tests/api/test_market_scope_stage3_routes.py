from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.types import DedupDiagnostics, ResolvedScope, ViewFamily
from pipeline.scripts.api.routes import market_scope


def test_options_route_returns_strategy_options_for_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(market_scope.router)
    client = TestClient(app)

    response = client.get("/api/market-scope/options", params={"brand": "트루패스", "view_family": "strategy", "source": "UBIST"})

    assert response.status_code == 200
    body = response.json()
    assert body["brand"] == "트루패스"
    assert "source:strategy_009" in {option["option_id"] for option in body["options"]}


def test_resolve_route_exposes_disjoint_dedup_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = _resolved_scope()

    class FakeResolver:
        def resolve(self, request: object) -> ResolvedScope:
            return resolved

    monkeypatch.setattr(market_scope, "build_strategy_resolver", lambda: FakeResolver())
    app = FastAPI()
    app.include_router(market_scope.router)
    client = TestClient(app)

    response = client.post(
        "/api/market-scope/resolve",
        json={
            "brand": "리바로젯",
            "view_family": "strategy",
            "source": "UBIST",
            "measure": "sales",
            "option_ids": ["group:livalo_family"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope_hash"] == "abc123"
    assert body["dedup"]["disjoint"] is True
    assert body["dedup"]["overlap_brand_key_count"] == 0


def test_cause_route_returns_portal_read_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResolver:
        def cause(self, request: object) -> dict[str, object]:
            return {
                "result": {
                    "brand": "리바로젯",
                    "source": "UBIST",
                    "market_meta": {"market_id": "strategy_006"},
                    "data": {"kpi": {"market_size_recent": 150.0}},
                },
                "resolved_scope": _resolved_scope().to_dict(),
            }

    monkeypatch.setattr(market_scope, "build_strategy_resolver", lambda: FakeResolver())
    app = FastAPI()
    app.include_router(market_scope.router)
    client = TestClient(app)

    response = client.post(
        "/api/market-scope/cause",
        json={
            "brand": "리바로젯",
            "view_family": "strategy",
            "source": "UBIST",
            "measure": "sales",
            "option_ids": ["group:livalo_family"],
            "view": "market_landscape",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["result"]["brand"] == "리바로젯"
    assert body["result"]["source"] == "UBIST"
    assert body["result"]["market_meta"]["market_id"] == "strategy_006"
    assert body["result"]["data"]["kpi"]["market_size_recent"] == 150.0
    assert "resolved_scope" not in body


def test_general_scope_routes_are_explicitly_not_ready() -> None:
    app = FastAPI()
    app.include_router(market_scope.router)
    client = TestClient(app)

    response = client.post(
        "/api/market-scope/resolve",
        json={
            "brand": "리바로젯",
            "view_family": "general",
            "source": "UBIST",
            "measure": "sales",
            "option_ids": ["general_atc4:C10C0"],
        },
    )

    assert response.status_code == 501
    assert response.json()["detail"]["error"] == "general_scope_not_ready"


def _resolved_scope() -> ResolvedScope:
    return ResolvedScope(
        scope_hash="abc123",
        view_family=ViewFamily.STRATEGY,
        selected_option_ids=("group:livalo_family",),
        resolved_source_markets=("strategy_006", "strategy_007"),
        resolved_atc4_set=("C10A1", "C10C0"),
        excluded_members=(),
        dedup=DedupDiagnostics(
            dedup_strategy="brand_key_disjoint_sum_v1",
            dedup_key_version="brand_key_market_guard_v1",
            candidate_fact_count=2,
            deduped_fact_count=2,
            dropped_duplicate_count=0,
            disjoint=True,
            overlap_brand_key_count=0,
        ),
        catalog_version="group_01_market_model_v1",
    )
