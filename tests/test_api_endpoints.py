from __future__ import annotations

import sys
from pathlib import Path
import json

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.scripts.api.main import app
from pipeline.scripts.api.db import connect


client = TestClient(app)


def test_cause_endpoint_joins_compact_cause_with_market_status() -> None:
    response = client.get("/api/cause/리바로?view=strategic_ml&source=ubist&measure=sales")

    assert response.status_code == 200
    body = response.json()

    assert body["brand_key"] == "리바로"
    assert body["market_id"] == "ml_006"
    assert "data" in body
    assert "market_cache_key" not in body

    assert "metric_history" not in body
    assert "ei_ms_matrix" not in body
    assert "brand_ranking_stacked" not in body
    assert "target_customer_competition" not in body

    data = body["data"]
    assert {"kpi", "sources_data", "by_dimension"} <= set(data)
    assert "metric_history" in data["sources_data"]
    assert "market_size_series" in data["sources_data"]
    assert "hhi_series_5y" in data["sources_data"]

    for key in [
        "brand_ranking_stacked",
        "company_ranking_stacked",
        "company_concentration_trend",
        "ei_ms_matrix",
        "growth_contribution_ms_matrix",
        "growth_contribution",
        "analysis_levels",
        "level_top5_trend",
        "target_customer_competition",
    ]:
        assert key in data


def test_four_split_cache_tables_exist_and_legacy_is_preserved() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'cache_%'")
            cache_tables = {next(iter(row.values())) for row in cur.fetchall()}

            cur.execute("SHOW TABLES LIKE 'response_store_legacy'")
            legacy_row = cur.fetchone()

    assert {
        "cache_brands",
        "cache_market_status",
        "cache_cause",
        "cache_deep_analysis",
    } <= cache_tables
    assert legacy_row is not None


def test_split_cache_row_counts_match_legacy_response_store() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT endpoint, COUNT(*) AS cnt FROM response_store_legacy GROUP BY endpoint")
            legacy_counts = {row["endpoint"]: row["cnt"] for row in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM cache_brands")
            brands_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM cache_market_status")
            market_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM cache_cause")
            cause_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM cache_deep_analysis")
            deep_count = cur.fetchone()["cnt"]

    assert brands_count == legacy_counts.get("brands")
    assert market_count == legacy_counts.get("market-status")
    assert cause_count == legacy_counts.get("cause")
    assert deep_count == legacy_counts.get("deep-analysis")


def test_cause_split_cache_payload_has_no_market_cache_key() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT response_json
                FROM cache_cause
                WHERE brand_key = '리바로'
                  AND view_type = 'strategic_ml'
                  AND source = 'ubist'
                  AND measure = 'sales'
                LIMIT 1
                """
            )
            row = cur.fetchone()

    assert row is not None
    cached = json.loads(row["response_json"])
    assert "market_cache_key" not in cached


def test_cause_endpoint_uses_market_id_when_brand_has_multiple_markets() -> None:
    response = client.get(
        "/api/cause/리바로?view=general&source=ubist&measure=sales&market_id=C10A1"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["brand_key"] == "리바로"
    assert body["market_id"] == "C10A1"


def test_deep_analysis_endpoint_returns_reference_shape_without_cause_embed() -> None:
    response = client.get(
        "/api/deep-analysis/리바로?view=strategic_ml&source=ubist&measure=sales"
    )

    assert response.status_code == 200
    body = response.json()

    assert body["brand_key"] == "리바로"
    assert body["market_id"] == "ml_006"
    assert {"forecast", "simulation", "events", "ai_analysis"} <= set(body["data"])
    assert "cause" not in body["data"]


def test_market_status_endpoint_returns_cache_source_of_truth() -> None:
    response = client.get(
        "/api/market-status/ml_006?view=strategic_ml&source=ubist&measure=sales"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["market_id"] == "ml_006"
    for key in [
        "market_size_series",
        "hhi_series_5y",
        "brand_ranking_stacked",
        "company_ranking_stacked",
        "company_concentration_trend",
        "ei_ms_matrix",
        "growth_contribution_ms_matrix",
        "growth_contribution",
        "analysis_levels",
        "level_top5_trend",
        "target_customer_competition",
    ]:
        assert key in body


def test_cause_endpoint_preserves_target_customer_competition_catalog_priority() -> None:
    response = client.get("/api/cause/라베칸?view=strategic_ml&source=ubist&measure=sales")

    assert response.status_code == 200
    tcc = response.json()["data"]["target_customer_competition"]
    assert tcc["source_type"] == "mixed"
    assert tcc["latest"]["top4"][0]["source"] == "catalog"


def test_health_endpoint_returns_ok() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
