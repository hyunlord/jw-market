from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
import shutil
import tempfile

import duckdb
import pandas as pd

from .brand_key_normalize import best_name, extract_brand_base_name, normalize_brand_name
from .general_catalog import _attach_catalog
from .general_config import LOGGER, enriched_glob, ubist_glob
from .general_utils import deduplicate_ubist_internal_medicine_rows, extract_atc4, ubist_channel_to_raw, ubist_specialty_to_raw


def _raw_ubist_aggregate_query(max_rows: int | None = None) -> str:
    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    return f"""
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
          first("성분") AS ubist_molecule_raw,
          first("성분용량") AS ubist_molecule_strength,
          first("제형") AS ubist_form,
          first("투여경로") AS ubist_route,
          first("급여구분") AS ubist_reimbursement,
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


def _normalize_raw_ubist_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame["source"] = "ubist"
    frame["audit_code"] = frame["product_code"].fillna("").astype(str)
    frame["display_priority_value"] = frame["raw_sales"]
    frame["brand_name"] = frame.apply(
        lambda row: best_name(
            extract_brand_base_name(row.get("product_name")),
            row.get("brand_name"),
            row.get("product_code"),
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


def iter_ubist_base_frames(
    *,
    max_rows: int | None = None,
    spool_dir: Path | None = None,
    partition_count: int = 64,
) -> Iterator[pd.DataFrame]:
    """Yield product-stable raw UBIST partitions without materializing the full aggregate."""
    if partition_count < 1:
        raise ValueError("partition_count must be positive")
    owned_spool = spool_dir is None
    root = Path(tempfile.mkdtemp(prefix="ubist-sidecar-")) if owned_spool else spool_dir
    assert root is not None
    parts = root / "parts"
    temp = root / "duckdb-temp"
    shutil.rmtree(parts, ignore_errors=True)
    shutil.rmtree(temp, ignore_errors=True)
    parts.mkdir(parents=True)
    temp.mkdir(parents=True)
    query = _raw_ubist_aggregate_query(max_rows)
    parts_sql = str(parts).replace("'", "''")
    temp_sql = str(temp).replace("'", "''")
    LOGGER.info("[ubist] spooling raw aggregate into %d product-stable partitions", partition_count)
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='4GB'")
        con.execute("SET threads=2")
        con.execute(f"SET temp_directory='{temp_sql}'")
        con.execute(
            f"""
            COPY (
              SELECT aggregated.*,
                     hash(COALESCE(product_code, '')) % {partition_count} AS __bucket
              FROM ({query}) AS aggregated
            ) TO '{parts_sql}' (
              FORMAT PARQUET,
              PARTITION_BY (__bucket)
            )
            """
        )
    finally:
        con.close()

    try:
        for partition in sorted(parts.glob("__bucket=*")):
            parquet_glob = str(partition / "*.parquet").replace("'", "''")
            partition_con = duckdb.connect()
            try:
                frame = partition_con.execute(f"SELECT * FROM read_parquet('{parquet_glob}')").df()
            finally:
                partition_con.close()
            yield _normalize_raw_ubist_frame(frame)
    finally:
        if owned_spool:
            shutil.rmtree(root, ignore_errors=True)

def load_ubist_base_frame(max_rows: int | None = None, ml: str | None = None) -> pd.DataFrame:
    if ml is None and os.environ.get("S4_INPUT_MODE", "raw") != "enriched":
        query = _raw_ubist_aggregate_query(max_rows)
        LOGGER.info("[ubist] aggregating raw UBIST parquet for all ATC4")
        con = duckdb.connect()
        try:
            frame = con.execute(query).df()
        finally:
            con.close()
        return _normalize_raw_ubist_frame(frame)

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
    dimension_map: dict[str, dict[str, object]] = {}
    if codes:
        con = duckdb.connect()
        con.register("codes", pd.DataFrame({"product_code": codes}))
        try:
            mapping = con.execute(
                f"""
                SELECT
                  CAST(u.약품코드 AS VARCHAR) AS product_code,
                  first(u.ATC) AS atc_text,
                  first(u.성분) AS ubist_molecule_raw,
                  first(u.성분용량) AS ubist_molecule_strength,
                  first(u.제형) AS ubist_form,
                  first(u.투여경로) AS ubist_route,
                  first(u.급여구분) AS ubist_reimbursement
                FROM read_parquet('{ubist_glob()}') AS u
                JOIN codes AS c ON CAST(u.약품코드 AS VARCHAR)=c.product_code
                GROUP BY 1
                """
            ).df()
        finally:
            con.close()
        atc_map = {row["product_code"]: extract_atc4(row["atc_text"]) for _, row in mapping.iterrows()}
        dimension_map = mapping.set_index("product_code")[
            ["ubist_molecule_raw", "ubist_molecule_strength", "ubist_form", "ubist_route", "ubist_reimbursement"]
        ].to_dict("index")
    atc = frame.apply(
        lambda row: atc_map.get(str(row.get("product_code")), (row.get("catalog_atc4_code") or "UNKNOWN", None)),
        axis=1,
    )
    frame["atc4_code"] = atc.map(lambda pair: pair[0])
    frame["atc4_desc"] = atc.map(lambda pair: pair[1])
    for column in ("ubist_molecule_raw", "ubist_molecule_strength", "ubist_form", "ubist_route", "ubist_reimbursement"):
        if codes:
            frame[column] = frame["product_code"].map(lambda code: dimension_map.get(str(code), {}).get(column))
        else:
            frame[column] = None
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = deduplicate_ubist_internal_medicine_rows(frame)
    return frame.loc[frame["brand_key"] != ""].copy()

def ubist_measure_frame(base: pd.DataFrame, measure: str) -> pd.DataFrame:
    frame = base.copy()
    frame["measure"] = measure
    frame["raw_value"] = frame["raw_sales"] if measure == "sales" else frame["raw_volume"]
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
