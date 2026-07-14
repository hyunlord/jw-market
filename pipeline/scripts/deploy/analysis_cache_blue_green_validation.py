"""Validate the staged analysis-block and brand-cache generation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Final

import pymysql

from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
)
from pipeline.scripts.deploy.analysis_cache_db import table_row_count, validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_exists


MALB_TABLE: Final[str] = "mart_analysis_level_block"
CACHE_BRANDS_TABLE: Final[str] = "cache_brands"
STAGING_TABLES: Final[dict[str, str]] = {
    MALB_TABLE: "mart_analysis_level_block_staging",
    CACHE_BRANDS_TABLE: "cache_brands_staging",
}
DEFAULT_EXPECTED_MALB_ROWS: Final[int] = 3138
DEFAULT_EXPECTED_BRAND_COUNT: Final[int] = 25
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StagingValidation:
    malb_rows: int
    malb_source_epoch: str
    cache_rows: int
    brand_count: int
    cache_sha256: str
    malb_build_version: str = ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION


def validate_staging_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    expected_brands_sha256: str,
    expected_source_epoch: str,
    expected_malb_rows: int = DEFAULT_EXPECTED_MALB_ROWS,
    expected_brand_count: int = DEFAULT_EXPECTED_BRAND_COUNT,
    expected_build_version: str = ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
) -> StagingValidation:
    validate_schema_name("target_db", target_db)
    _validate_expectations(
        expected_brands_sha256=expected_brands_sha256,
        expected_source_epoch=expected_source_epoch,
        expected_malb_rows=expected_malb_rows,
        expected_brand_count=expected_brand_count,
    )
    for staging_table in STAGING_TABLES.values():
        if not table_exists(conn, target_db, staging_table):
            raise RuntimeError(f"staging table missing: {target_db}.{staging_table}")

    malb_table = STAGING_TABLES[MALB_TABLE]
    malb_rows = table_row_count(conn, target_db, malb_table)
    if malb_rows != expected_malb_rows:
        raise RuntimeError(
            f"MALB staging row count mismatch: {malb_rows} != {expected_malb_rows}"
        )
    source_epoch, build_version = _malb_identity(conn, target_db, malb_table)
    if source_epoch != expected_source_epoch:
        raise RuntimeError(
            f"MALB staging source epoch mismatch: {source_epoch} != {expected_source_epoch}"
        )
    if build_version != expected_build_version:
        raise RuntimeError(
            f"MALB staging build version mismatch: {build_version} != {expected_build_version}"
        )

    cache_table = STAGING_TABLES[CACHE_BRANDS_TABLE]
    cache_rows = table_row_count(conn, target_db, cache_table)
    if cache_rows != 1:
        raise RuntimeError(f"cache_brands staging row count mismatch: {cache_rows} != 1")
    payload = _read_cache_brands_payload(conn, target_db, cache_table)
    cache_sha256 = validate_cache_brands_payload(
        payload,
        expected_sha256=expected_brands_sha256,
        expected_brand_count=expected_brand_count,
    )
    return StagingValidation(
        malb_rows=malb_rows,
        malb_source_epoch=source_epoch,
        cache_rows=cache_rows,
        brand_count=len(payload),
        cache_sha256=cache_sha256,
        malb_build_version=build_version,
    )


def validate_cache_brands_payload(
    payload: object,
    *,
    expected_sha256: str,
    expected_brand_count: int,
) -> str:
    if not isinstance(payload, list):
        raise RuntimeError("cache_brands default payload must be a list")
    if len(payload) != expected_brand_count:
        raise RuntimeError(
            f"cache_brands brand count mismatch: {len(payload)} != {expected_brand_count}"
        )
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise RuntimeError(f"cache_brands item {index} must be an object")
        missing = [
            key
            for key in ("general_sources", "strategic_sources")
            if key not in item
        ]
        if missing:
            raise RuntimeError(f"cache_brands item {index} missing keys: {missing}")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(
            f"cache_brands canonical sha mismatch: {digest} != {expected_sha256}"
        )
    return digest


def _validate_expectations(
    *,
    expected_brands_sha256: str,
    expected_source_epoch: str,
    expected_malb_rows: int,
    expected_brand_count: int,
) -> None:
    if not SHA256_RE.fullmatch(expected_brands_sha256):
        raise ValueError("expected_brands_sha256 must be a lowercase SHA256")
    if not expected_source_epoch:
        raise ValueError("expected_source_epoch must be non-empty")
    if expected_malb_rows <= 0:
        raise ValueError("expected_malb_rows must be positive")
    if expected_brand_count <= 0:
        raise ValueError("expected_brand_count must be positive")


def _malb_identity(
    conn: pymysql.connections.Connection,
    target_db: str,
    table_name: str,
) -> tuple[str, str]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(DISTINCT source_epoch) AS epoch_count, "
            f"MIN(source_epoch) AS source_epoch, "
            f"COUNT(DISTINCT build_version) AS build_version_count, "
            f"MIN(build_version) AS build_version FROM "
            f"{quote_id(target_db)}.{quote_id(table_name)}"
        )
        row = cursor.fetchone()
    epoch_count = int((row or {}).get("epoch_count") or 0)
    source_epoch = str((row or {}).get("source_epoch") or "")
    build_version_count = int((row or {}).get("build_version_count") or 0)
    build_version = str((row or {}).get("build_version") or "")
    if epoch_count != 1 or not source_epoch:
        raise RuntimeError(
            f"MALB staging must contain exactly one source epoch, found {epoch_count}"
        )
    if build_version_count != 1 or not build_version:
        raise RuntimeError(
            "MALB staging must contain exactly one build version, "
            f"found {build_version_count}"
        )
    return source_epoch, build_version


def _read_cache_brands_payload(
    conn: pymysql.connections.Connection,
    target_db: str,
    table_name: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT query_key, response_json FROM "
            f"{quote_id(target_db)}.{quote_id(table_name)} ORDER BY query_key"
        )
        rows = list(cursor.fetchall())
    if len(rows) != 1 or str(rows[0].get("query_key") or "") != "default":
        raise RuntimeError("cache_brands staging must contain only the default row")
    raw = rows[0].get("response_json")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("cache_brands staging contains invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("cache_brands staging default payload must be a list")
    return payload
