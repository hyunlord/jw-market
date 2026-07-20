from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.capabilities_registry import (
    _GROUP_MODELS,
    _METRIC_LABELS,
    VIEWS,
    build_capabilities,
    metric_field_ids,
)
from pipeline.scripts.api.routes import capabilities


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(capabilities.router)
    return TestClient(app)


def test_endpoint_returns_200_and_shape() -> None:
    resp = _client().get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"]
    assert body["api_version"]
    groups = {g["group"] for g in body["metric_groups"]}
    assert groups == {"cause", "dynamic-market", "deep-analysis"}
    for group in body["metric_groups"]:
        assert group["metrics"], f"{group['group']} must expose metrics"
        for metric in group["metrics"]:
            assert set(metric) == {"id", "label", "unit"}
            assert metric["label"]
            assert metric["unit"] in {"pct", "value", "krw", "index", "score", "rank"}


def test_view_enum_is_canonical_triple() -> None:
    body = build_capabilities()
    assert body["views"]["enum"] == ["general", "strategic_ml", "strategic_cd"]
    assert tuple(body["views"]["enum"]) == VIEWS
    aliases = body["views"]["legacy_aliases"]
    assert aliases["market_landscape"] == "strategic_ml"
    assert aliases["competitive_dynamics"] == "strategic_cd"


def test_market_id_retained_not_deprecated() -> None:
    body = build_capabilities()
    market_id = body["identifiers"]["market_id"]
    assert market_id["deprecated"] is False
    assert market_id["status"] == "retained"
    deprecated_fields = {entry["field"] for entry in body["deprecated"]}
    assert "market_id" not in deprecated_fields
    assert "view_kind" in deprecated_fields


def test_period_anchors_match_period_window() -> None:
    body = build_capabilities()
    anchors = {anchor["id"] for anchor in body["period"]["anchors"]}
    assert anchors == {"annual", "quarter", "month"}


def test_provenance_declares_source_epoch_and_built_at() -> None:
    body = build_capabilities()
    assert body["provenance"]["source_epoch"]["status"] == "supported"
    assert body["provenance"]["built_at"]["status"] == "planned"


# --- G-5: the registry is introspection-validated against the real models ----------

def test_no_numeric_model_field_is_left_unlabeled() -> None:
    """Adding a KPI float field to a response model must force a registry entry."""
    missing: list[str] = []
    for group, models in _GROUP_MODELS.items():
        for field_id in metric_field_ids(models):
            if field_id not in _METRIC_LABELS:
                missing.append(f"{group}:{field_id}")
    assert not missing, f"unlabeled metric fields (registry drift): {missing}"


def test_published_metric_ids_are_real_model_fields() -> None:
    body = build_capabilities()
    for group in body["metric_groups"]:
        real_fields: set[str] = set()
        for model in _GROUP_MODELS[group["group"]]:
            real_fields |= set(model.model_fields)
        for metric in group["metrics"]:
            assert metric["id"] in real_fields, f"{group['group']}.{metric['id']} not a model field"
