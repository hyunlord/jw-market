#!/usr/bin/env python3
"""Audit why UBIST general brand mart loaded 4,910 rows."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import duckdb
import pandas as pd

from brand_key_normalize import best_name, normalize_brand_name
from layer3_compute_general_v3 import mariadb_connect
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
RAW_GLOB = str(PROJECT_ROOT / "output" / "ubist" / "year=*" / "month=*" / "data.parquet")
ENRICHED_GLOB = str(PROJECT_ROOT / "output" / "enriched" / "ml_id=*" / "data.parquet")
STRATEGIC_PRODUCT_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_product" / "strategic_product.parquet"


ATC_RE = re.compile(r"\[([A-Z0-9]+)\]")


def extract_atc4(value: str | None) -> str | None:
    if not value:
        return None
    match = ATC_RE.search(str(value))
    if match:
        return match.group(1)
    token = str(value).strip().split(" ", 1)[0]
    return token[:5] if token else None


def duckdb_counts() -> dict[str, int]:
    con = duckdb.connect()
    con.create_function("normalize_brand_name", normalize_brand_name, ["VARCHAR"], "VARCHAR")
    con.create_function("extract_atc4", extract_atc4, ["VARCHAR"], "VARCHAR")
    try:
        raw = con.execute(
            f"""
            SELECT
              COUNT(DISTINCT 브랜드) AS raw_distinct_brand,
              COUNT(DISTINCT normalize_brand_name(브랜드)) AS raw_normalized_brand,
              COUNT(DISTINCT normalize_brand_name(브랜드) || '|' || COALESCE(extract_atc4(ATC), '')) AS raw_brand_atc4
            FROM read_parquet('{RAW_GLOB}')
            WHERE 브랜드 IS NOT NULL
            """
        ).fetchone()
        layer2_products = con.execute(
            f"""
            SELECT DISTINCT product_id
            FROM read_parquet('{ENRICHED_GLOB}')
            WHERE source = 'ubist'
              AND product_id IS NOT NULL
              AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)
            """
        ).df()
    finally:
        con.close()

    products = pd.read_parquet(STRATEGIC_PRODUCT_PATH).rename(columns={"name": "product_name", "merge_name": "brand_name"})
    keep = [col for col in ("product_id", "product_name", "brand_name") if col in products.columns]
    product_map = products[keep].drop_duplicates("product_id")
    layer2 = layer2_products.merge(product_map, on="product_id", how="left")
    layer2["display_name"] = layer2.apply(
        lambda row: best_name(row.get("brand_name"), row.get("product_name"), row.get("product_id")),
        axis=1,
    )
    layer2["brand_key"] = layer2["display_name"].map(normalize_brand_name)
    return {
        "raw_distinct_brand": raw[0],
        "raw_normalized_brand": raw[1],
        "raw_brand_atc4": raw[2],
        "layer2_distinct_product": int(layer2["product_id"].nunique()),
        "layer2_normalized_brand": int(layer2["brand_key"].nunique()),
        "layer2_unmapped_product": int(layer2["brand_name"].isna().sum()),
    }


def mart_counts() -> dict[str, object]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(DISTINCT brand_key) AS distinct_brand_key,
                       COUNT(DISTINCT atc4_code) AS distinct_atc4,
                       COUNT(DISTINCT CONCAT(brand_key, '|', atc4_code)) AS distinct_brand_atc4
                FROM mart_general_brand_metric
                WHERE source='ubist'
                """
            )
            total = dict(cur.fetchone())
            cur.execute(
                """
                SELECT measure, COUNT(*) AS row_count, COUNT(DISTINCT brand_key) AS distinct_brand
                FROM mart_general_brand_metric
                WHERE source='ubist'
                GROUP BY measure
                ORDER BY measure
                """
            )
            by_measure = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT DISTINCT brand_key FROM mart_general_brand_metric WHERE source='ubist'")
            mart_keys = {row["brand_key"] for row in cur.fetchall()}
    finally:
        conn.close()
    return {"total": total, "by_measure": by_measure, "mart_key_count": len(mart_keys)}


def main() -> int:
    result = {"duckdb": duckdb_counts(), "mart": mart_counts()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
