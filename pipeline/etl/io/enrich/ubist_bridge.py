from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from pipeline.etl.io.enrich.normalize import clean_scalar
from pipeline.etl.io.enrich.schema import ENRICHED_COLUMNS
from pipeline.etl.io.ubist_specialties import aggregate_specialty_labels


KST = ZoneInfo("Asia/Seoul")


def now_iso(ingested_at: str | None = None) -> str:
    return ingested_at or datetime.now(KST).isoformat(timespec="seconds")


def duckdb_product_key_expr(expr: str) -> str:
    return (
        "lower(regexp_replace("
        f"replace(replace(replace(cast({expr} as varchar), '㎎', 'mg'), 'ＭＧ', 'mg'), ' ', ''), "
        "'\\\\s+', '', 'g'))"
    )


def duckdb_case_map(
    expr: str,
    mapping: dict[str, str],
    default: str = "Unknown",
    contains: bool = False,
) -> str:
    clauses = []
    for raw, canonical in mapping.items():
        raw_sql = str(raw).replace("'", "''")
        val_sql = str(canonical).replace("'", "''")
        if contains:
            clauses.append(f"WHEN cast({expr} as varchar) LIKE '%{raw_sql}%' THEN '{val_sql}'")
        else:
            clauses.append(f"WHEN cast({expr} as varchar) = '{raw_sql}' THEN '{val_sql}'")
    return f"CASE {' '.join(clauses)} ELSE '{default}' END"


def duckdb_excludes_catalog_values(expr: str, values: frozenset[str]) -> str:
    """Build an exact-match exclusion predicate for catalogued raw labels."""
    escaped_values = (value.replace("'", "''") for value in sorted(values))
    literals = ", ".join(f"'{value}'" for value in escaped_values)
    if not literals:
        return "TRUE"
    return f"coalesce(trim(cast({expr} as varchar)), '') NOT IN ({literals})"


def register_products(con: duckdb.DuckDBPyConnection, products: pd.DataFrame) -> None:
    rows: list[dict[str, object]] = []
    for record in products.to_dict("records"):
        key_pairs = {
            clean_scalar(record.get("ubist_product_key")): clean_scalar(record.get("ubist_product_title")),
            clean_scalar(record.get("iqvia_product_key")): clean_scalar(record.get("iqvia_product_title")),
        }
        for key, title in key_pairs.items():
            if not key:
                continue
            rows.append(
                {
                    "product_id": record["product_id"],
                    "ml_id": record["ml_id"],
                    "product_title": title,
                    "product_key": key,
                    "brand_key": record.get("brand_key"),
                    "strength_bracket_code": record.get("strength_bracket_code"),
                }
            )
    bridge = pd.DataFrame(rows).drop_duplicates()
    con.register("product_bridge", bridge)


def ubist_join_sql(customer_dict: dict[str, object], *, ubist_glob: str, ingested_at: str | None = None) -> str:
    product_key = duckdb_product_key_expr("u.제품")
    channel_case = duckdb_case_map("u.종별", customer_dict.get("ubist_channel", {}), default="Unknown")
    specialty_case = duckdb_case_map(
        "u.진료과",
        customer_dict.get("ubist_specialty", {}),
        default="Unknown",
        contains=True,
    )
    specialty_filter = duckdb_excludes_catalog_values(
        "u.진료과",
        aggregate_specialty_labels(customer_dict),
    )
    ingested = now_iso(ingested_at).replace("'", "''")
    return (
        "SELECT DISTINCT "
        "p.ml_id AS ml_id, "
        "p.product_id AS product_id, "
        "'ubist' AS source, "
        "u.period_yyyymm AS period_yyyymm, "
        "try_cast(u.rx_amt AS DOUBLE) AS raw_rx_amt, "
        "try_cast(u.rx_cnt AS DOUBLE) AS raw_rx_cnt, "
        "try_cast(u.rx_qty AS DOUBLE) AS raw_rx_qty, "
        "try_cast(u.rx_amt AS DOUBLE) AS canonical_value, "
        f"{channel_case} AS channel, "
        f"{specialty_case} AS specialty, "
        "'product_name_exact' AS match_method, "
        "'high' AS match_confidence, "
        "'ubist_parquet' AS source_table, "
        "concat("
        "'ubist::', "
        "coalesce(cast(u.source_file AS varchar), ''), "
        "'::', "
        "coalesce(cast(u.source_sheet AS varchar), ''), "
        "'::', "
        "coalesce(cast(u.source_row_no AS varchar), ''), "
        "'::', "
        "coalesce(cast(u.period_yyyymm AS varchar), ''), "
        "'::', "
        "coalesce(cast(u.약품코드 AS varchar), '')"
        ") AS source_row_id, "
        f"'{ingested}' AS ingested_at "
        f"FROM read_parquet('{ubist_glob}') AS u "
        f"JOIN product_bridge AS p ON {product_key} = p.product_key "
        f"WHERE {specialty_filter}"
    )


def merge_parquet_sources(output_path: Path, frames: list[pd.DataFrame]) -> tuple[int, int]:
    existing: list[pd.DataFrame] = []
    if output_path.exists():
        existing.append(pd.read_parquet(output_path))
    for frame in frames:
        if not frame.empty:
            existing.append(frame)
    df = pd.concat(existing, ignore_index=True) if existing else pd.DataFrame(columns=ENRICHED_COLUMNS)
    for col in ENRICHED_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df.reindex(columns=ENRICHED_COLUMNS)
    for col in ["raw_rx_amt", "raw_rx_cnt", "raw_rx_qty", "canonical_value"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in [
        "ml_id",
        "product_id",
        "source",
        "period_yyyymm",
        "channel",
        "specialty",
        "match_method",
        "match_confidence",
        "source_table",
        "source_row_id",
        "ingested_at",
    ]:
        df[col] = df[col].fillna("").astype(str)
    tmp = output_path.with_suffix(".tmp.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
    tmp.replace(output_path)
    return int(len(df)), int(df["product_id"].nunique() if not df.empty else 0)


def write_empty_ml(output_path: Path) -> tuple[int, int]:
    return merge_parquet_sources(output_path, [])


def write_ubist_ml(
    products: pd.DataFrame,
    customer_dict: dict[str, object],
    output_path: Path,
    *,
    ubist_glob: str,
    ingested_at: str | None = None,
) -> tuple[int, int]:
    con = duckdb.connect()
    register_products(con, products)
    sql = ubist_join_sql(customer_dict, ubist_glob=ubist_glob, ingested_at=ingested_at)
    tmp = output_path.with_suffix(".tmp.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    tmp.replace(output_path)
    stats = con.execute(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT product_id) AS products FROM read_parquet('{output_path}')"
    ).fetchone()
    con.close()
    return int(stats[0] or 0), int(stats[1] or 0)
