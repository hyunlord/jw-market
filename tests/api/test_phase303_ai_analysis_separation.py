from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pymysql

from pipeline.scripts.etl.cache_build_common import CANONICAL_25


BASE_URL = "http://127.0.0.1:8013"
JW25 = list(CANONICAL_25)


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _deep_payload(brand: str) -> dict:
    encoded = urllib.parse.quote(brand)
    with urllib.request.urlopen(f"{BASE_URL}/api/deep-analysis/{encoded}", timeout=30) as response:
        payload = json.load(response)
    assert payload.get("data"), payload
    return payload


def test_phase303_ai_analysis_table_has_all_phase_zeta_markers() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES LIKE 'cache_deep_analysis_ai_analysis'")
        assert cur.fetchone(), "cache_deep_analysis_ai_analysis table is missing"
        placeholders = ",".join(["%s"] * len(JW25))
        cur.execute(
            f"""
            SELECT brand,
                   JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.phase_zeta_stage')) AS stage,
                   JSON_EXTRACT(ai_analysis_json, '$.phenomenon') AS phenomenon,
                   JSON_EXTRACT(ai_analysis_json, '$.cause') AS cause,
                   JSON_EXTRACT(ai_analysis_json, '$.prediction') AS prediction,
                   JSON_EXTRACT(ai_analysis_json, '$.recommendation') AS recommendation
            FROM cache_deep_analysis_ai_analysis
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            JW25,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_brand = {row["brand"]: row for row in rows}
    assert set(by_brand) == set(JW25)
    for brand in JW25:
        row = by_brand[brand]
        assert row["stage"], brand
        for section in ("phenomenon", "cause", "prediction", "recommendation"):
            assert row[section], (brand, section)


def test_phase303_backend_merges_ai_analysis_from_dedicated_table() -> None:
    payload = _deep_payload("가드메트")
    ai = payload["data"].get("ai_analysis") or {}
    assert ai.get("phase_zeta_stage"), ai
    assert ai.get("phenomenon", {}).get("title"), ai


def test_phase303_base_cache_does_not_store_ai_analysis_key() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT brand, JSON_CONTAINS_PATH(response_json, 'one', '$.data.ai_analysis') AS has_ai
            FROM cache_deep_analysis
            WHERE brand IN ('가드메트', '리바로', '헴리브라')
            ORDER BY brand
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    assert rows
    assert all(int(row["has_ai"] or 0) == 0 for row in rows), rows
