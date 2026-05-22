"""Cache grain checks for Phase 2 spec-aligned rebuild."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from layer3_compute_general_v3 import mariadb_connect


CANONICAL_25 = {
    "라베칸",
    "라베칸듀오",
    "제이클",
    "가드렛",
    "가드메트",
    "타발리스",
    "시그마트",
    "리바로",
    "리바로젯",
    "리바로페노",
    "리바로하이",
    "리바로브이",
    "트루패스",
    "피나스타",
    "제이다트",
    "뉴트로진",
    "모빌리아",
    "악템라",
    "페린젝트",
    "베노훼럼",
    "헴리브라",
    "위너프",
    "위너프A+",
    "엔커버",
    "플라주오피",
}


def scalar(sql: str):
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return next(iter(row.values()))
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


def test_cache_brands_default_has_25_canonical_brands():
    assert scalar("SELECT JSON_LENGTH(response_json) FROM cache_brands WHERE query_key='default'") == 25


def test_cache_market_status_has_kpi_and_25_cards():
    result = rows(
        """
        SELECT JSON_EXTRACT(response_json, '$.kpi.ubist') AS ubist,
               JSON_EXTRACT(response_json, '$.kpi.iqvia') AS iqvia,
               JSON_LENGTH(response_json, '$.brand_cards') AS cards
        FROM cache_market_status
        WHERE query_key='default'
        """
    )[0]
    assert result["ubist"] is not None
    assert result["iqvia"] is not None
    assert result["cards"] == 25


def test_cache_cause_preserves_phase1_mart_grain():
    cache_total = scalar("SELECT COUNT(*) FROM cache_cause")
    mart_total = scalar(
        """
        SELECT
          (SELECT COUNT(*) FROM mart_strategic_ml_brand_metric) +
          (SELECT COUNT(*) FROM mart_strategic_cd_brand_metric)
        """
    )
    assert cache_total == mart_total == 8838


def test_cache_deep_analysis_uses_brand_single_pk():
    result = rows("SELECT COUNT(*) AS total, COUNT(DISTINCT brand) AS unique_brand FROM cache_deep_analysis")[0]
    assert result["total"] == result["unique_brand"]


def test_cache_deep_analysis_contains_canonical_25():
    found = {
        row["brand"]
        for row in rows(
            """
            SELECT brand
            FROM cache_deep_analysis
            WHERE brand IN ('라베칸','라베칸듀오','제이클','가드렛','가드메트','타발리스',
                            '시그마트','리바로','리바로젯','리바로페노','리바로하이','리바로브이',
                            '트루패스','피나스타','제이다트','뉴트로진','모빌리아','악템라',
                            '페린젝트','베노훼럼','헴리브라','위너프','위너프A+','엔커버','플라주오피')
            """
        )
    }
    assert found == CANONICAL_25
