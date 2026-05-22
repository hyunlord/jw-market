"""Strategic mart completeness checks for canonical market grain."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from layer3_compute_general_v3 import mariadb_connect  # noqa: E402
from layer3_compute_strategic_ml_v3 import expected_measure_pairs  # noqa: E402


def _fetch_pairs(table: str, id_column: str, id_value: str) -> set[tuple[str, str]]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT source, measure, COUNT(DISTINCT brand_key) AS brand_count
                FROM {table}
                WHERE {id_column}=%s
                GROUP BY source, measure
                """,
                (id_value,),
            )
            return {
                (row["source"], row["measure"])
                for row in cur.fetchall()
                if int(row["brand_count"]) > 0
            }
    finally:
        conn.close()


def _fetch_distinct(sql: str, params: tuple[object, ...] = ()) -> set[str]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {str(next(iter(row.values()))) for row in cur.fetchall()}
    finally:
        conn.close()


def test_all_16_ml_markets_have_expected_source_measure_pairs() -> None:
    ml_market = pd.read_parquet("output/catalog/ml_market/ml_market.parquet")

    assert ml_market["ml_id"].nunique() == 16
    for _, market in ml_market.iterrows():
        expected = expected_measure_pairs(market["data_source"])
        actual = _fetch_pairs("mart_strategic_ml_brand_metric", "ml_id", market["ml_id"])
        assert expected <= actual, f"{market['ml_id']} missing {sorted(expected - actual)}"


def test_all_19_cd_markets_have_expected_source_measure_pairs() -> None:
    cd_market = pd.read_parquet("output/catalog/cd_market/cd_market.parquet")

    assert cd_market["cd_id"].nunique() == 19
    for _, market in cd_market.iterrows():
        expected = expected_measure_pairs(market["data_source"])
        actual = _fetch_pairs("mart_strategic_cd_brand_metric", "cd_market_id", market["cd_id"])
        assert expected <= actual, f"{market['cd_id']} missing {sorted(expected - actual)}"


def test_ml003_and_ml015_dual_source_regressions_are_fixed() -> None:
    for ml_id in ("ml_003", "ml_015"):
        actual = _fetch_pairs("mart_strategic_ml_brand_metric", "ml_id", ml_id)
        assert expected_measure_pairs("both") <= actual

    ml003_brands = _fetch_distinct(
        """
        SELECT DISTINCT brand_name
        FROM mart_strategic_ml_brand_metric
        WHERE ml_id='ml_003' AND is_jw=1
        """
    )
    assert {"가드렛", "가드메트"} <= ml003_brands

    ml015_brands = _fetch_distinct(
        """
        SELECT DISTINCT brand_name
        FROM mart_strategic_ml_brand_metric
        WHERE ml_id='ml_015' AND is_jw=1
        """
    )
    assert "엔커버" in ml015_brands


def test_25_canonical_brands_are_loaded_in_ml_mart() -> None:
    brands = _fetch_distinct(
        """
        SELECT DISTINCT brand_name
        FROM mart_strategic_ml_brand_metric
        WHERE is_jw=1
        """
    )
    assert len(brands) == 25
    assert {"가드렛", "가드메트", "리바로젯", "리바로페노", "위너프A+"} <= brands


def test_non_jw_market_members_are_preserved() -> None:
    members = _fetch_distinct(
        """
        SELECT DISTINCT brand_name
        FROM mart_strategic_ml_brand_metric
        WHERE is_jw=0
          AND brand_name IN ('위너프페리','위너프에이플러스페리','라베가드','에소가드','나도가드','자이가드','하모닐란')
        """
    )
    assert {"위너프페리", "위너프에이플러스페리", "라베가드", "에소가드", "나도가드", "자이가드", "하모닐란"} <= members
