from __future__ import annotations

import os

import duckdb
import pandas as pd

from .brand_key_normalize import best_name, extract_brand_base_name, normalize_brand_name
from .general_catalog import _attach_catalog
from .general_config import LOGGER, enriched_glob, ubist_glob
from .general_utils import deduplicate_ubist_internal_medicine_rows, extract_atc4, ubist_channel_to_raw, ubist_specialty_to_raw

def load_ubist_base_frame(max_rows: int | None = None, ml: str | None = None) -> pd.DataFrame:
    if ml is None and os.environ.get("S4_INPUT_MODE", "raw") != "enriched":
        limit = f"LIMIT {int(max_rows)}" if max_rows else ""
        query = f"""
            SELECT
              CAST("약품코드" AS VARCHAR) AS product_code,
              first("제품") AS product_name,
              first("브랜드") AS brand_name,
              first("ATC") AS atc_text,
              period_yyyymm,
              "종별" AS channel,
              "진료과" AS specialty,
              first("제조사") AS manufacturer,
              first("판매사") AS company,
              SUM(TRY_CAST(rx_amt AS DOUBLE)) AS raw_sales,
              SUM(TRY_CAST(rx_qty AS DOUBLE)) AS raw_volume
            FROM (
              SELECT *
              FROM read_parquet('{ubist_glob()}', hive_partitioning=true)
              WHERE TRY_CAST(rx_amt AS DOUBLE) > 0 OR TRY_CAST(rx_qty AS DOUBLE) > 0
              {limit}
            ) AS u
            GROUP BY 1,5,6,7
        """
        LOGGER.info("[ubist] aggregating raw UBIST parquet for all ATC4")
        con = duckdb.connect()
        try:
            frame = con.execute(query).df()
        finally:
            con.close()
        frame["source"] = "ubist"
        frame["audit_code"] = frame["product_code"].fillna("").astype(str)
        frame["display_priority_value"] = frame["raw_sales"]
        frame["brand_name"] = frame.apply(
            lambda r: best_name(
                extract_brand_base_name(r.get("product_name")),
                r.get("brand_name"),
                r.get("product_code"),
            ),
            axis=1,
        )
        frame["brand_key"] = frame["brand_name"].map(normalize_brand_name)
        atc = frame["atc_text"].map(extract_atc4)
        frame["atc4_code"] = atc.map(lambda pair: pair[0])
        frame["atc4_desc"] = atc.map(lambda pair: pair[1])
        frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
        frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
        frame = deduplicate_ubist_internal_medicine_rows(frame)
        return frame.loc[frame["brand_key"] != ""].copy()

    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    parquet_glob = enriched_glob(ml)
    query = f"""
        SELECT
          ml_id,
          product_id,
          split_part(source_row_id, '::', 6) AS product_code,
          period_yyyymm,
          channel,
          specialty,
          SUM(CAST(raw_rx_amt AS DOUBLE)) AS raw_sales,
          SUM(CAST(raw_rx_qty AS DOUBLE)) AS raw_volume
        FROM (
          SELECT *
          FROM read_parquet('{parquet_glob}')
          WHERE source='ubist' AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)
          {limit}
        ) AS e
        GROUP BY 1,2,3,4,5,6
    """
    LOGGER.info("[ubist] aggregating Layer 2 enriched parquet")
    con = duckdb.connect()
    try:
        frame = con.execute(query).df()
    finally:
        con.close()
    frame = _attach_catalog(frame)
    frame["source"] = "ubist"
    frame["audit_code"] = frame["product_code"].fillna("").astype(str)
    frame["display_priority_value"] = frame["raw_sales"]
    codes = [code for code in frame["product_code"].dropna().astype(str).unique().tolist() if code]
    atc_map: dict[str, tuple[str, str | None]] = {}
    if codes:
        con = duckdb.connect()
        con.register("codes", pd.DataFrame({"product_code": codes}))
        try:
            mapping = con.execute(
                f"""
                SELECT CAST(u.약품코드 AS VARCHAR) AS product_code, first(u.ATC) AS atc_text
                FROM read_parquet('{ubist_glob()}') AS u
                JOIN codes AS c ON CAST(u.약품코드 AS VARCHAR)=c.product_code
                GROUP BY 1
                """
            ).df()
        finally:
            con.close()
        atc_map = {row["product_code"]: extract_atc4(row["atc_text"]) for _, row in mapping.iterrows()}
    atc = frame.apply(
        lambda row: atc_map.get(str(row.get("product_code")), (row.get("catalog_atc4_code") or "UNKNOWN", None)),
        axis=1,
    )
    frame["atc4_code"] = atc.map(lambda pair: pair[0])
    frame["atc4_desc"] = atc.map(lambda pair: pair[1])
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = deduplicate_ubist_internal_medicine_rows(frame)
    return frame.loc[frame["brand_key"] != ""].copy()

def ubist_measure_frame(base: pd.DataFrame, measure: str) -> pd.DataFrame:
    frame = base.copy()
    frame["measure"] = measure
    frame["raw_value"] = frame["raw_sales"] if measure == "sales" else frame["raw_volume"]
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
