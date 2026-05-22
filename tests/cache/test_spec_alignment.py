"""Spec alignment checks for Phase 2 cache tables."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from layer3_compute_general_v3 import mariadb_connect


def distinct_values(sql: str) -> set[str]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return {next(iter(row.values())) for row in cur.fetchall()}
    finally:
        conn.close()


def rows(sql: str):
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def test_cache_cause_has_only_spec_view_values():
    assert distinct_values("SELECT DISTINCT view_type FROM cache_cause") == {
        "market_landscape",
        "competitive_dynamics",
    }


def test_cache_cause_has_only_uppercase_sources():
    assert distinct_values("SELECT DISTINCT source FROM cache_cause") == {"UBIST", "IQVIA"}


def test_cache_cause_uses_strategy_market_ids():
    assert distinct_values("SELECT DISTINCT market_id FROM cache_cause") == {f"strategy_{i:03d}" for i in range(1, 17)}


def test_cache_cause_has_no_general_view():
    assert rows("SELECT COUNT(*) AS c FROM cache_cause WHERE view_type='general'")[0]["c"] == 0


def test_competitive_dynamics_keeps_brand_specific_cd_branching():
    data = {
        row["brand"]: row["market_size"]
        for row in rows(
            """
            SELECT brand, JSON_EXTRACT(response_json, '$.data.kpi.market_size_recent') AS market_size
            FROM cache_cause
            WHERE brand IN ('리바로하이', '리바로브이')
              AND view_type='competitive_dynamics'
              AND source='UBIST'
              AND measure='sales'
            """
        )
    }
    assert set(data) == {"리바로하이", "리바로브이"}
    assert data["리바로하이"] != data["리바로브이"]
