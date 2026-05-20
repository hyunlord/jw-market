#!/usr/bin/env python3
"""Collect read-only data-state metadata for the JW market viewer."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow.parquet as pq
import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.ops_utils import find_project_root, first_existing


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
ENV_PATH = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")

MART_TABLES = [
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
]
CATALOG_TABLES = [
    "ml_market",
    "cd_market",
    "cd_filter",
    "strategic_brand",
    "strategic_product",
    "cd_brand",
    "cd_product",
]
IQVIA_RAW_CANDIDATES = ["staging_iqvia_nsa", "iqvia_nsa_quarterly_raw"]
JW_BRANDS = [
    "리바로",
    "가드메트",
    "페린젝트",
    "시그마트",
    "리바로페노",
    "리바로젯",
    "시그마트레지스",
    "타발리스",
    "리바로페노에프",
]
TEXT_TYPES = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "json"}
LARGE_TEXT_TYPES = {"text", "tinytext", "mediumtext", "longtext", "json"}
SKIP_DISTINCT_TYPES = {"blob", "tinyblob", "mediumblob", "longblob"}


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_db_conn(*, port: int | None = None, cursorclass=pymysql.cursors.DictCursor) -> pymysql.connections.Connection:
    env = load_env()
    password = env.get("MARIADB_PASSWORD")
    if not password:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {ENV_PATH}")
    return pymysql.connect(
        host="127.0.0.1",
        port=port or int(env.get("HOST_PORT", "3308")),
        user=env.get("MARIADB_USER", "jwapp"),
        password=password,
        database=env.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=cursorclass,
    )


def quote_identifier(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return "`" + identifier.replace("`", "``") + "`"


def json_safe(value: Any, *, max_string_length: int = 5_000) -> Any:
    """Convert Python, pandas, and DB scalar values into JSON-safe values."""
    if isinstance(value, dict):
        return {str(k): json_safe(v, max_string_length=max_string_length) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, max_string_length=max_string_length) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item"):
        try:
            return json_safe(value.item(), max_string_length=max_string_length)
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, str) and len(value) > max_string_length:
        return value[:max_string_length] + f"... (+{len(value) - max_string_length} chars truncated)"
    return value


def rows_json_safe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json_safe(dict(row)) for row in rows]


def get_git_commit(project_root: Path = PROJECT_ROOT) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project_root, text=True).strip()
    except subprocess.CalledProcessError:
        return "unknown"


def get_git_tag(project_root: Path = PROJECT_ROOT) -> str:
    try:
        tags = subprocess.check_output(["git", "tag", "--points-at", "HEAD"], cwd=project_root, text=True).strip().splitlines()
    except subprocess.CalledProcessError:
        return ""
    return ", ".join(tags)


def parquet_files(pattern: str, project_root: Path = PROJECT_ROOT) -> list[Path]:
    return sorted(project_root.glob(pattern))


def parquet_row_count(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def read_parquet_sample(path: Path, *, limit: int = 5_000) -> pd.DataFrame:
    parquet = pq.ParquetFile(path)
    batches = parquet.iter_batches(batch_size=limit)
    try:
        batch = next(batches)
    except StopIteration:
        return pd.read_parquet(path).head(0)
    return batch.to_pandas().head(limit)


def schema_from_frame(df: pd.DataFrame, *, stats_scope: str = "full") -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    row_count = len(df)
    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        null_rate = (float(series.isnull().mean()) * 100) if row_count else 0.0
        unique_count = int(non_null.nunique(dropna=True)) if row_count else 0
        sample_values = [str(json_safe(v))[:80] for v in non_null.unique()[:3]]
        schema.append(
            {
                "name": str(col),
                "type": str(series.dtype),
                "nullable": True,
                "null_rate": round(null_rate, 2),
                "unique_count": unique_count,
                "sample_values": sample_values,
                "stats_scope": stats_scope,
            }
        )
    return schema


def frame_records(df: pd.DataFrame, *, limit: int = 20) -> list[dict[str, Any]]:
    return rows_json_safe(df.head(limit).to_dict(orient="records"))


def find_columns(columns: Iterable[str], tokens: Iterable[str]) -> list[str]:
    lowered = {column: column.lower() for column in columns}
    return [column for column, lower in lowered.items() if any(token.lower() in lower for token in tokens)]


def find_brand_rows(df: pd.DataFrame, *, limit: int = 50) -> pd.DataFrame:
    if df.empty:
        return df.head(0)
    preferred = find_columns(df.columns, ["brand", "name", "제품", "브랜드", "상품"])
    candidate_columns = preferred or [col for col in df.columns if df[col].dtype == "object"]
    if not candidate_columns:
        return df.head(0)
    mask = pd.Series(False, index=df.index)
    for col in candidate_columns:
        text = df[col].astype(str)
        for brand in JW_BRANDS:
            mask = mask | text.str.contains(brand, regex=False, na=False)
    return df[mask].head(limit)


def find_jw_rows_in_parquet(files: list[Path], *, limit: int = 50, max_files: int = 8, batch_size: int = 50_000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files[:max_files]:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=batch_size):
            df = batch.to_pandas()
            jw_df = find_brand_rows(df, limit=limit - len(rows))
            if not jw_df.empty:
                rows.extend(frame_records(jw_df, limit=limit - len(rows)))
            if len(rows) >= limit:
                return rows[:limit]
    return rows[:limit]


def load_jw_product_lookup(project_root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for catalog_name in ("strategic_product", "cd_product"):
        path = project_root / "output" / "catalog" / catalog_name / f"{catalog_name}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        jw_df = find_brand_rows(df, limit=len(df))
        for row in jw_df.to_dict(orient="records"):
            product_id = row.get("product_id")
            if not product_id or product_id in lookup:
                continue
            lookup[str(product_id)] = {
                "jw_product_name": json_safe(row.get("name")),
                "jw_brand_id": json_safe(row.get("brand_id")),
                "jw_ml_id": json_safe(row.get("ml_id")),
                "jw_cd_id": json_safe(row.get("cd_id")),
            }
    return lookup


def find_enriched_jw_rows(
    files: list[Path],
    product_lookup: dict[str, dict[str, Any]],
    *,
    limit: int = 50,
    batch_size: int = 50_000,
) -> list[dict[str, Any]]:
    if not product_lookup:
        return []
    product_ids = set(product_lookup)
    rows: list[dict[str, Any]] = []
    preferred_files = sorted(
        files,
        key=lambda path: 0 if path.parent.name.replace("ml_id=", "") in {"ml_006", "ml_012"} else 1,
    )
    for path in preferred_files:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=batch_size):
            df = batch.to_pandas()
            if "product_id" not in df.columns:
                continue
            jw_df = df[df["product_id"].astype(str).isin(product_ids)].head(limit - len(rows))
            for record in jw_df.to_dict(orient="records"):
                product_id = str(record.get("product_id"))
                annotated = {**product_lookup.get(product_id, {}), **record}
                rows.append(json_safe(annotated))
            if len(rows) >= limit:
                return rows[:limit]
    return rows[:limit]


def counter_distribution(df: pd.DataFrame, columns: list[str], *, limit: int = 20) -> list[dict[str, Any]]:
    if df.empty:
        return []
    existing = [col for col in columns if col in df.columns]
    if not existing:
        return []
    grouped = df.groupby(existing, dropna=False).size().reset_index(name="count").sort_values("count", ascending=False)
    return rows_json_safe(grouped.head(limit).to_dict(orient="records"))


def collect_ubist_raw(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    files = parquet_files("output/ubist/year=*/month=*/data.parquet", project_root)
    if not files:
        return {
            "layer": "layer_1_raw",
            "purpose": "ubist",
            "error": "file_not_found",
            "path": "output/ubist/year=*/month=*/data.parquet",
            "total_rows": 0,
            "total_columns": 0,
            "schema": [],
            "sample_rows": [],
            "jw_deep_sample": [],
            "distribution": {},
            "storage_info": {},
        }

    partition_rows = []
    total_rows = 0
    total_size = 0
    for path in files:
        rows = parquet_row_count(path)
        total_rows += rows
        total_size += path.stat().st_size
        year = path.parent.parent.name.replace("year=", "")
        month = path.parent.name.replace("month=", "")
        partition_rows.append({"period": f"{year}-{month}", "rows": rows})

    sample_df = read_parquet_sample(files[0])
    channel_columns = find_columns(sample_df.columns, ["channel", "종별", "요양기관종별", "유통"])
    specialty_columns = find_columns(sample_df.columns, ["specialty", "진료과", "과목"])
    distribution = {
        "period_distribution": partition_rows,
    }
    channel_distribution = counter_distribution(sample_df, (channel_columns[:1] + specialty_columns[:1]), limit=20)
    if channel_distribution:
        distribution["channel_distribution_sample"] = channel_distribution

    return {
        "layer": "layer_1_raw",
        "purpose": "ubist",
        "total_rows": total_rows,
        "total_columns": len(sample_df.columns),
        "schema": schema_from_frame(sample_df, stats_scope=f"sample_first_partition_n={len(sample_df):,}"),
        "sample_rows": frame_records(sample_df, limit=20),
        "jw_deep_sample": find_jw_rows_in_parquet(files, limit=30),
        "distribution": distribution,
        "storage_info": {
            "partition_count": len(files),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "stats_basis": f"{files[0].relative_to(project_root)} first {len(sample_df):,} rows",
        },
    }


def collect_enriched_layer2(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    files = parquet_files("output/enriched/ml_id=*/data.parquet", project_root)
    if not files:
        return {
            "layer": "layer_2_enriched",
            "purpose": "enriched",
            "error": "file_not_found",
            "path": "output/enriched/ml_id=*/data.parquet",
            "total_rows": 0,
            "total_columns": 0,
            "schema": [],
            "sample_rows": [],
            "jw_deep_sample": [],
            "distribution": {},
            "storage_info": {},
        }

    partition_breakdown = []
    total_rows = 0
    total_size = 0
    for path in files:
        rows = parquet_row_count(path)
        total_rows += rows
        total_size += path.stat().st_size
        partition_breakdown.append(
            {
                "ml_id": path.parent.name.replace("ml_id=", ""),
                "rows": rows,
                "file_size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            }
        )

    preferred = project_root / "output" / "enriched" / "ml_id=ml_006" / "data.parquet"
    sample_path = preferred if preferred.exists() else files[0]
    sample_df = read_parquet_sample(sample_path)

    product_lookup = load_jw_product_lookup(project_root)
    return {
        "layer": "layer_2_enriched",
        "purpose": "enriched",
        "total_rows": total_rows,
        "total_columns": len(sample_df.columns),
        "schema": schema_from_frame(sample_df, stats_scope=f"sample_{sample_path.parent.name}_n={len(sample_df):,}"),
        "sample_rows": frame_records(sample_df, limit=20),
        "jw_deep_sample": find_enriched_jw_rows(files, product_lookup, limit=50),
        "distribution": {"partition_breakdown": partition_breakdown},
        "storage_info": {
            "partition_count": len(files),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "sample_partition": str(sample_path.relative_to(project_root)),
        },
    }


def table_exists(cur: pymysql.cursors.DictCursor, table_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table_name,),
    )
    return int(cur.fetchone()["cnt"]) > 0


def get_db_schema(cur: pymysql.cursors.DictCursor, table_name: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table_name,),
    )
    return [
        {
            "name": row["COLUMN_NAME"],
            "type": row["DATA_TYPE"],
            "nullable": row["IS_NULLABLE"] == "YES",
        }
        for row in cur.fetchall()
    ]


def enrich_db_column_stats(
    cur: pymysql.cursors.DictCursor,
    table_name: str,
    schema: list[dict[str, Any]],
    total_rows: int,
    *,
    sample_limit: int | None = None,
) -> list[dict[str, Any]]:
    table = quote_identifier(table_name)
    for col in schema:
        name = col["name"]
        data_type = str(col["type"]).lower()
        column = quote_identifier(name)
        if data_type in SKIP_DISTINCT_TYPES:
            col.update({"null_rate": 0.0, "unique_count": 0, "sample_values": ["<binary>"]})
            continue

        if sample_limit:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS sample_rows,
                    SUM(CASE WHEN sample_value IS NULL THEN 1 ELSE 0 END) AS null_count,
                    COUNT(DISTINCT sample_value) AS unique_count
                FROM (
                    SELECT {column} AS sample_value
                    FROM {table}
                    LIMIT %s
                ) AS sampled_values
                """,
                (sample_limit,),
            )
            stat = cur.fetchone()
            sample_rows = int(stat["sample_rows"] or 0)
            null_count = int(stat["null_count"] or 0)
            unique_count = int(stat["unique_count"] or 0)
            null_rate = (null_count / sample_rows * 100) if sample_rows else 0.0
            cur.execute(
                f"""
                SELECT sample_value
                FROM (
                    SELECT {column} AS sample_value
                    FROM {table}
                    LIMIT %s
                ) AS sampled_values
                WHERE sample_value IS NOT NULL
                LIMIT 3
                """,
                (sample_limit,),
            )
            sample_values = [str(json_safe(row["sample_value"]))[:80] for row in cur.fetchall()]
            col.update(
                {
                    "null_rate": round(null_rate, 2),
                    "unique_count": unique_count,
                    "sample_values": sample_values,
                    "stats_scope": f"sample_first_{sample_rows}_rows",
                }
            )
            continue

        if data_type in LARGE_TEXT_TYPES:
            unique_sql = f"""
                SELECT COUNT(DISTINCT sample_value) AS unique_count
                FROM (
                    SELECT {column} AS sample_value
                    FROM {table}
                    WHERE {column} IS NOT NULL
                    LIMIT 5000
                ) AS sampled_values
            """
            unique_scope = "sample_first_5000_non_null"
        else:
            unique_sql = f"SELECT COUNT(DISTINCT {column}) AS unique_count FROM {table}"
            unique_scope = "full_table"

        cur.execute(f"SELECT SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS null_count FROM {table}")
        null_stat = cur.fetchone()
        cur.execute(unique_sql)
        unique_stat = cur.fetchone()
        null_count = int(null_stat["null_count"] or 0)
        unique_count = int(unique_stat["unique_count"] or 0)
        null_rate = (null_count / total_rows * 100) if total_rows else 0.0

        cur.execute(
            f"""
            SELECT {column} AS sample_value
            FROM {table}
            WHERE {column} IS NOT NULL
            LIMIT 3
            """
        )
        sample_values = [str(json_safe(row["sample_value"]))[:80] for row in cur.fetchall()]
        if data_type == "json":
            sample_values = [value[:80] for value in sample_values] or ["<JSON>"]
        col.update(
            {
                "null_rate": round(null_rate, 2),
                "unique_count": unique_count,
                "sample_values": sample_values,
                "stats_scope": unique_scope,
            }
        )
    return schema


def db_sample_rows(cur: pymysql.cursors.DictCursor, table_name: str, *, limit: int = 20) -> list[dict[str, Any]]:
    cur.execute(f"SELECT * FROM {quote_identifier(table_name)} LIMIT %s", (limit,))
    return rows_json_safe(cur.fetchall())


def find_db_brand_columns(schema: list[dict[str, Any]]) -> list[str]:
    columns = [col["name"] for col in schema if str(col["type"]).lower() in TEXT_TYPES]
    preferred = find_columns(columns, ["brand_name", "brand", "name", "product", "payload"])
    return preferred or columns[:3]


def db_jw_deep_sample(
    cur: pymysql.cursors.DictCursor,
    table_name: str,
    schema: list[dict[str, Any]],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    brand_columns = [col for col in find_db_brand_columns(schema) if col != "payload"][:4]
    if not brand_columns:
        return []
    clauses = []
    params: list[str | int] = []
    for column_name in brand_columns:
        column = quote_identifier(column_name)
        for brand in JW_BRANDS:
            clauses.append(f"CAST({column} AS CHAR) LIKE %s")
            params.append(f"%{brand}%")
    if not clauses:
        return []
    params.append(limit)
    cur.execute(
        f"""
        SELECT *
        FROM {quote_identifier(table_name)}
        WHERE {" OR ".join(clauses)}
        LIMIT %s
        """,
        params,
    )
    return rows_json_safe(cur.fetchall())


def db_distribution(
    cur: pymysql.cursors.DictCursor,
    table_name: str,
    schema: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    names = {col["name"] for col in schema}
    candidates = [
        ("source_measure", ["source", "measure"]),
        ("ml_id", ["ml_id"]),
        ("cd_id", ["cd_id"]),
        ("aggregation_level", ["aggregation_level"]),
    ]
    distribution: dict[str, list[dict[str, Any]]] = {}
    table = quote_identifier(table_name)
    for key, columns in candidates:
        if not all(col in names for col in columns):
            continue
        select_cols = ", ".join(quote_identifier(col) for col in columns)
        group_cols = select_cols
        cur.execute(
            f"""
            SELECT {select_cols}, COUNT(*) AS count
            FROM {table}
            GROUP BY {group_cols}
            ORDER BY count DESC
            LIMIT 30
            """
        )
        distribution[key] = rows_json_safe(cur.fetchall())
    return distribution


def get_mart_storage_info(cur: pymysql.cursors.DictCursor, table_name: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, UPDATE_TIME, ENGINE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table_name,),
    )
    row = cur.fetchone() or {}
    data_length = int(row.get("DATA_LENGTH") or 0)
    index_length = int(row.get("INDEX_LENGTH") or 0)
    return {
        "table_name": table_name,
        "engine": row.get("ENGINE"),
        "estimated_rows": int(row.get("TABLE_ROWS") or 0),
        "size_mb": round((data_length + index_length) / 1024 / 1024, 2),
        "updated_at": json_safe(row.get("UPDATE_TIME")),
    }


def collect_mart(mart_name: str, *, conn: pymysql.connections.Connection | None = None) -> dict[str, Any]:
    own_conn = conn is None
    conn = conn or get_db_conn()
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, mart_name):
                return {
                    "layer": "layer_3_mart",
                    "purpose": "mart",
                    "error": "table_not_found",
                    "table_name": mart_name,
                    "total_rows": 0,
                    "total_columns": 0,
                    "schema": [],
                    "sample_rows": [],
                    "jw_deep_sample": [],
                    "distribution": {},
                    "storage_info": {},
                }
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {quote_identifier(mart_name)}")
            total_rows = int(cur.fetchone()["cnt"])
            schema = enrich_db_column_stats(cur, mart_name, get_db_schema(cur, mart_name), total_rows)
            return {
                "layer": "layer_3_mart",
                "purpose": "mart",
                "total_rows": total_rows,
                "total_columns": len(schema),
                "schema": schema,
                "sample_rows": db_sample_rows(cur, mart_name, limit=20),
                "jw_deep_sample": db_jw_deep_sample(cur, mart_name, schema, limit=50),
                "distribution": db_distribution(cur, mart_name, schema),
                "storage_info": get_mart_storage_info(cur, mart_name),
            }
    finally:
        if own_conn:
            conn.close()


def collect_catalog(catalog_name: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = project_root / "output" / "catalog" / catalog_name / f"{catalog_name}.parquet"
    if not path.exists():
        return {
            "layer": "catalog",
            "purpose": "catalog",
            "error": "file_not_found",
            "path": str(path.relative_to(project_root)),
            "total_rows": 0,
            "total_columns": 0,
            "schema": [],
            "sample_rows": [],
            "jw_deep_sample": [],
            "distribution": {},
            "storage_info": {},
        }
    df = pd.read_parquet(path)
    distribution: dict[str, list[dict[str, Any]]] = {}
    for columns in (["ml_id"], ["cd_id"], ["source"], ["market_type"]):
        if all(col in df.columns for col in columns):
            distribution["_".join(columns)] = counter_distribution(df, columns, limit=20)
    return {
        "layer": "catalog",
        "purpose": "catalog",
        "total_rows": len(df),
        "total_columns": len(df.columns),
        "schema": schema_from_frame(df, stats_scope="full_table"),
        "sample_rows": frame_records(df, limit=20),
        "jw_deep_sample": frame_records(find_brand_rows(df, limit=50), limit=50),
        "distribution": distribution,
        "storage_info": {
            "parquet_path": str(path.relative_to(project_root)),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
        },
    }


def select_iqvia_table(cur: pymysql.cursors.DictCursor) -> str | None:
    for table_name in IQVIA_RAW_CANDIDATES:
        if table_exists(cur, table_name):
            return table_name
    return None


def db_payload_jw_deep_sample(
    cur: pymysql.cursors.DictCursor,
    table_name: str,
    schema: list[dict[str, Any]],
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    names = {col["name"] for col in schema}
    if "payload" not in names:
        return db_jw_deep_sample(cur, table_name, schema, limit=limit)
    clauses = []
    params: list[str | int] = []
    for brand in JW_BRANDS:
        clauses.append("CAST(`payload` AS CHAR) LIKE %s")
        params.append(f"%{brand}%")
    params.append(limit)
    cur.execute(
        f"""
        SELECT *
        FROM {quote_identifier(table_name)}
        WHERE {" OR ".join(clauses)}
        LIMIT %s
        """,
        params,
    )
    return rows_json_safe(cur.fetchall())


def collect_iqvia_raw(*, conn: pymysql.connections.Connection | None = None) -> dict[str, Any]:
    own_conn = conn is None
    conn = conn or get_db_conn()
    try:
        with conn.cursor() as cur:
            table_name = select_iqvia_table(cur)
            if not table_name:
                return {
                    "layer": "layer_1_raw",
                    "purpose": "iqvia",
                    "error": "table_not_found",
                    "candidates": IQVIA_RAW_CANDIDATES,
                    "total_rows": 0,
                    "total_columns": 0,
                    "schema": [],
                    "sample_rows": [],
                    "jw_deep_sample": [],
                    "distribution": {},
                    "storage_info": {},
                }
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {quote_identifier(table_name)}")
            total_rows = int(cur.fetchone()["cnt"])
            schema = enrich_db_column_stats(cur, table_name, get_db_schema(cur, table_name), total_rows, sample_limit=5_000)
            return {
                "layer": "layer_1_raw",
                "purpose": "iqvia",
                "total_rows": total_rows,
                "total_columns": len(schema),
                "schema": schema,
                "sample_rows": db_sample_rows(cur, table_name, limit=20),
                "jw_deep_sample": db_payload_jw_deep_sample(cur, table_name, schema, limit=30),
                "distribution": db_distribution(cur, table_name, schema),
                "storage_info": get_mart_storage_info(cur, table_name),
            }
    finally:
        if own_conn:
            conn.close()


def layer_totals(tables: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for info in tables.values():
        totals[str(info.get("layer", "unknown"))] += int(info.get("total_rows") or 0)
    return dict(totals)


def collect_all(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    state: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo_commit": get_git_commit(project_root),
        "repo_tag": get_git_tag(project_root),
        "tables": {},
    }
    tables: dict[str, dict[str, Any]] = state["tables"]

    tables["ubist_raw"] = collect_ubist_raw(project_root)
    with get_db_conn() as conn:
        tables["iqvia_nsa_raw"] = collect_iqvia_raw(conn=conn)
        tables["enriched_parquet"] = collect_enriched_layer2(project_root)
        for mart_name in MART_TABLES:
            tables[mart_name] = collect_mart(mart_name, conn=conn)

    for catalog_name in CATALOG_TABLES:
        tables[catalog_name] = collect_catalog(catalog_name, project_root=project_root)

    state["total_rows"] = sum(int(table.get("total_rows") or 0) for table in tables.values())
    state["layer_totals"] = layer_totals(tables)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    args = parser.parse_args()

    state = collect_all()
    payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(args.output)
    else:
        print(payload)


if __name__ == "__main__":
    main()
