"""Response skeleton checks for Phase 2 cache JSON payloads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from layer3_compute_general_v3 import mariadb_connect


def get_json(sql: str):
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return json.loads(next(iter(row.values())))
    finally:
        conn.close()


def test_cache_cause_response_contains_spec_top_level_and_data_keys():
    payload = get_json(
        """
        SELECT response_json
        FROM cache_cause
        WHERE brand='가드렛'
          AND view_type='market_landscape'
          AND source='UBIST'
          AND measure='sales'
          AND market_id='strategy_003'
        LIMIT 1
        """
    )
    assert {"brand", "market_id", "view", "source", "measure", "unit_label", "data", "market_meta"} <= set(payload)
    assert {
        "kpi",
        "sources_data",
        "ei_ms_matrix",
        "growth_contribution",
        "level_top5_trend",
        "target_customer_competition",
        "brand_ranking",
        "company_ranking",
        "company_concentration_trend",
    } <= set(payload["data"])


def test_deep_analysis_combines_dual_source_measure_combos():
    payload = get_json("SELECT response_json FROM cache_deep_analysis WHERE brand='가드렛'")
    combos = set(payload["data"]["forecast"]["by_combo"])
    assert combos == {
        "UBIST.sales",
        "UBIST.volume",
        "IQVIA.sales",
        "IQVIA.unit",
        "IQVIA.dosage_unit",
        "IQVIA.counting_unit",
    }
    assert set(payload["data"]) == {"forecast", "simulation", "events"}
