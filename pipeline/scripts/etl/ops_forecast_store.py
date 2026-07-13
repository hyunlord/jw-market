"""Staging persistence and safety gates for monthly forecast generation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder
from pipeline.scripts.etl.ops_forecast_scope import BlockRow, HorizonRow

LIVE_BLOCK: Final[str] = "deep_forecast_block"
LIVE_HORIZON: Final[str] = "deep_forecast_horizon"


def epoch_is_current(connection: Any, table: str, source_epoch: str, expected_count: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT source_epoch) AS epoch_count, "
            f"MIN(source_epoch) AS source_epoch FROM {general_builder.quote_ident(table)}"
        )
        row = cursor.fetchone()
    return int(row["row_count"]) == expected_count and int(row["epoch_count"]) == 1 and str(row["source_epoch"]) == source_epoch


def mart_source_epoch(connection: Any) -> str:
    tables = (
        "mart_general_brand_metric",
        "mart_general_market_metric",
        "mart_strategic_ml_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_cd_market_metric",
    )
    fingerprint: list[tuple[str, int, str]] = []
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                f"SELECT COUNT(*) AS row_count, COALESCE(MAX(computed_at), '') AS computed_at "
                f"FROM {general_builder.quote_ident(table)}"
            )
            row = cursor.fetchone()
            fingerprint.append((table, int(row["row_count"]), str(row["computed_at"])))
    return hashlib.sha256(json.dumps(fingerprint, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def prepare_staging(connection: Any, block_table: str, horizon_table: str, source_epoch: str) -> None:
    if block_table == LIVE_BLOCK or horizon_table == LIVE_HORIZON:
        raise RuntimeError("live forecast tables are forbidden in staging mode")
    with connection.cursor() as cursor:
        for staging, live in ((block_table, LIVE_BLOCK), (horizon_table, LIVE_HORIZON)):
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {general_builder.quote_ident(staging)} "
                f"LIKE {general_builder.quote_ident(live)}"
            )
            cursor.execute(
                f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT source_epoch) AS epoch_count, "
                f"MIN(source_epoch) AS source_epoch FROM {general_builder.quote_ident(staging)}"
            )
            row = cursor.fetchone()
            if int(row["row_count"]) and (int(row["epoch_count"]) != 1 or str(row["source_epoch"]) != source_epoch):
                cursor.execute(f"TRUNCATE TABLE {general_builder.quote_ident(staging)}")
    connection.commit()


def existing_block_keys(connection: Any, table: str) -> set[tuple[str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT brand_key, source, market_id FROM {general_builder.quote_ident(table)}")
        return {(str(row["brand_key"]), str(row["source"]), str(row["market_id"])) for row in cursor.fetchall()}


def existing_horizon_keys(connection: Any, table: str) -> set[tuple[str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT market_id, source, measure FROM {general_builder.quote_ident(table)}")
        return {(str(row["market_id"]), str(row["source"]), str(row["measure"])) for row in cursor.fetchall()}


def insert_blocks(connection: Any, table: str, rows: list[BlockRow]) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {general_builder.quote_ident(table)} (brand_key, source, market_id, view_kind, "
        "forecast_json, simulation_json, generation_status, no_history_fallback, simulation_available, "
        "source_epoch, source_computed_at, generated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = [tuple(row.__dict__.values()) if hasattr(row, "__dict__") else (
        row.brand_key, row.source, row.market_id, row.view_kind, row.forecast_json, row.simulation_json,
        row.generation_status, row.no_history_fallback, row.simulation_available, row.source_epoch,
        row.source_computed_at, row.generated_at,
    ) for row in rows]
    with connection.cursor() as cursor:
        inserted = cursor.executemany(sql, values)
    connection.commit()
    return int(inserted or 0)


def insert_horizons(connection: Any, table: str, rows: list[HorizonRow]) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {general_builder.quote_ident(table)} (market_id, source, measure, view_kind, "
        "forecast_horizon_json, source_row_count, source_epoch, source_computed_at, generated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = [(
        row.market_id, row.source, row.measure, row.view_kind, row.forecast_horizon_json,
        row.source_row_count, row.source_epoch, row.source_computed_at, row.generated_at,
    ) for row in rows]
    with connection.cursor() as cursor:
        inserted = cursor.executemany(sql, values)
    connection.commit()
    return int(inserted or 0)


def completion_gate(connection: Any, block_table: str, horizon_table: str, expected_blocks: int, expected_horizons: int) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS n, SUM(simulation_available = 0 AND simulation_json IS NOT NULL "
            f"OR simulation_available = 1 AND simulation_json IS NULL) AS bad_simulation "
            f"FROM {general_builder.quote_ident(block_table)}"
        )
        block = cursor.fetchone()
        cursor.execute(f"SELECT COUNT(*) AS n FROM {general_builder.quote_ident(horizon_table)}")
        horizon = cursor.fetchone()
    counts = {"blocks": int(block["n"]), "horizons": int(horizon["n"]), "bad_simulation": int(block["bad_simulation"] or 0)}
    if counts != {"blocks": expected_blocks, "horizons": expected_horizons, "bad_simulation": 0}:
        raise RuntimeError(f"forecast completion gate failed: {counts}")
    return counts


def contamination_count(connection: Any, block_table: str) -> int:
    sql = f"""
        WITH native_brands AS (
            SELECT 'general' view_kind, atc4_code market_id, source, brand_name FROM mart_general_brand_metric
            UNION SELECT 'market_landscape', ml_id, source, brand_name FROM mart_strategic_ml_brand_metric
            UNION SELECT 'competitive_dynamics', cd_market_id, source, brand_name FROM mart_strategic_cd_brand_metric
        )
        SELECT COUNT(*) AS invalid_count
        FROM {general_builder.quote_ident(block_table)} block_row
        JOIN JSON_TABLE(
            block_row.forecast_json,
            '$.by_combo.*.brands[*]' COLUMNS (brand_name VARCHAR(255) PATH '$.brand')
        ) payload_brand
        LEFT JOIN native_brands native
          ON native.view_kind = block_row.view_kind AND native.market_id = block_row.market_id
         AND native.source = block_row.source
         AND native.brand_name = payload_brand.brand_name COLLATE utf8mb4_unicode_ci
        WHERE native.brand_name IS NULL
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return int(row["invalid_count"])
