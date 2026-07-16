#!/usr/bin/env python3
"""Migrate generated general forecasts into source-scoped serving tables.

DEPRECATED: this was the one-shot 2026-07 unified-table migration with pinned
source row counts (34,378 / 2,880). It is retained for provenance only; do not
run it against a newer mart generation. Regeneration paths are
``ops_forecast_builder.py`` / ``general_forecast_full_generation.py`` /
``strategic_forecast_full_generation.py``. See BRANCH_POLICY.md for the
historical-lineage policy. Removal requires a separate PL decision.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import groupby
import json
import os
from pathlib import Path
import sys
from typing import Any, Final, Iterable, Iterator

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.utils.mart_config import DEFAULT_MART_DB_NAME
from pipeline.scripts.etl.cache_build_common import dump_payload


DATABASE: Final = DEFAULT_MART_DB_NAME
SOURCE_EPOCH: Final = DATABASE
SOURCE_BLOCK_TABLE: Final = "cache_deep_analysis_general"
SOURCE_HORIZON_TABLE: Final = "cache_market_forecast_general"
BLOCK_TABLE: Final = "deep_forecast_block"
HORIZON_TABLE: Final = "deep_forecast_horizon"
SOURCE_PREFIX: Final = {"IQVIA": "iqvia_nsa", "UBIST": "ubist"}
BATCH_SIZE: Final = 25


@dataclass(frozen=True, slots=True)
class BlockRow:
    brand_key: str
    source: str
    market_id: str
    view_kind: str
    forecast_json: str
    simulation_json: str | None
    generation_status: str | None
    no_history_fallback: str | None
    simulation_available: bool
    source_epoch: str
    source_computed_at: datetime | None
    generated_at: datetime
    generated_at_source: str


@dataclass(frozen=True, slots=True)
class HorizonRow:
    market_id: str
    source: str
    measure: str
    view_kind: str
    forecast_horizon_json: str
    source_row_count: int
    source_epoch: str
    source_computed_at: datetime | None
    generated_at: datetime


def compact_json(value: Any) -> str:
    return dump_payload(value)


def derive_view_kind(market_id: str) -> str:
    if market_id.startswith("ml_"):
        return "market_landscape"
    if market_id.startswith("cd_"):
        return "competitive_dynamics"
    if market_id and ":" not in market_id and market_id.replace("-", "").isalnum():
        return "general"
    raise ValueError(f"unsupported market_id: {market_id!r}")


def _source_for_combo(combo: str) -> str:
    prefix = combo.split(".", 1)[0]
    try:
        return SOURCE_PREFIX[prefix]
    except KeyError as exc:
        raise ValueError(f"unsupported forecast combo: {combo!r}") from exc


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _generated_at(payload: dict[str, Any], updated_at: datetime) -> tuple[datetime, str]:
    payload_value = _parse_datetime(payload.get("generated_at"))
    if payload_value is not None:
        return payload_value, "payload.generated_at"
    return updated_at, "source.updated_at_fallback"


def _filter_combo_dict(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if _source_for_combo(str(key)) == source}


def _section_for_source(section: Any, source: str) -> dict[str, Any]:
    result = deepcopy(section) if isinstance(section, dict) else {}
    result["by_combo"] = _filter_combo_dict(result.get("by_combo"), source)
    return result


def split_block_payload(
    *,
    brand_key: str,
    market_id: str,
    response_json: str,
    source_computed_at: datetime | None,
    updated_at: datetime,
) -> list[BlockRow]:
    payload = json.loads(response_json)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    forecast = data.get("forecast") if isinstance(data.get("forecast"), dict) else {}
    simulation = data.get("simulation") if isinstance(data.get("simulation"), dict) else {}
    forecast_combos = forecast.get("by_combo") if isinstance(forecast.get("by_combo"), dict) else {}
    sources = sorted({_source_for_combo(str(combo)) for combo in forecast_combos})
    if not sources:
        raise ValueError(f"forecast has no source combos: {brand_key}/{market_id}")
    generation_status = payload.get("generation_status")
    fallback = payload.get("no_history_fallback")
    availability = payload.get("simulation_available")
    generated_at, generated_at_source = _generated_at(payload, updated_at)
    result: list[BlockRow] = []
    for source in sources:
        source_availability = _filter_combo_dict(availability, source)
        simulation_available = any(bool(value) for value in source_availability.values())
        source_simulation = _section_for_source(simulation, source)
        result.append(
            BlockRow(
                brand_key=brand_key,
                source=source,
                market_id=market_id,
                view_kind=derive_view_kind(market_id),
                forecast_json=compact_json(_section_for_source(forecast, source)),
                simulation_json=compact_json(source_simulation) if simulation_available else None,
                generation_status=str(generation_status) if generation_status is not None else None,
                no_history_fallback=(
                    compact_json(_filter_combo_dict(fallback, source)) if isinstance(fallback, dict) else None
                ),
                simulation_available=simulation_available,
                source_epoch=SOURCE_EPOCH,
                source_computed_at=source_computed_at,
                generated_at=generated_at,
                generated_at_source=generated_at_source,
            )
        )
    return result


def reassemble_block_payload(original_json: str, rows: Iterable[BlockRow]) -> str:
    payload = json.loads(original_json)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    original_simulation = data.get("simulation") if isinstance(data.get("simulation"), dict) else {}
    forecast_by_combo: dict[str, Any] = {}
    simulation_by_combo: dict[str, Any] = {}
    fallback: dict[str, Any] = {}
    availability: dict[str, Any] = {}
    generation_status: str | None = None
    for row in rows:
        forecast = json.loads(row.forecast_json)
        forecast_by_combo.update(forecast.get("by_combo") or {})
        if row.simulation_json is not None:
            simulation = json.loads(row.simulation_json)
            simulation_by_combo.update(simulation.get("by_combo") or {})
        else:
            for combo, value in (original_simulation.get("by_combo") or {}).items():
                if _source_for_combo(str(combo)) == row.source:
                    simulation_by_combo[combo] = value
        if row.no_history_fallback is not None:
            fallback.update(json.loads(row.no_history_fallback))
        original_availability = payload.get("simulation_available")
        if isinstance(original_availability, dict):
            availability.update(_filter_combo_dict(original_availability, row.source))
        generation_status = row.generation_status
    forecast = deepcopy(data.get("forecast") or {})
    forecast["by_combo"] = forecast_by_combo
    simulation = deepcopy(original_simulation)
    simulation["by_combo"] = simulation_by_combo
    data["forecast"] = forecast
    data["simulation"] = simulation
    payload["data"] = data
    if generation_status is not None:
        payload["generation_status"] = generation_status
    if isinstance(payload.get("no_history_fallback"), dict):
        payload["no_history_fallback"] = fallback
    if isinstance(payload.get("simulation_available"), dict):
        payload["simulation_available"] = availability
    return compact_json(payload)


def create_table_statements() -> tuple[str, str]:
    view_check = """CHECK (
            (view_kind = 'market_landscape' AND market_id LIKE 'ml\\_%') OR
            (view_kind = 'competitive_dynamics' AND market_id LIKE 'cd\\_%') OR
            (view_kind = 'general' AND market_id NOT LIKE 'ml\\_%' AND market_id NOT LIKE 'cd\\_%')
        )"""
    block = f"""
        CREATE TABLE {BLOCK_TABLE} (
            brand_key VARCHAR(255) NOT NULL,
            source VARCHAR(16) NOT NULL,
            market_id VARCHAR(64) NOT NULL,
            view_kind VARCHAR(32) NOT NULL,
            forecast_json LONGTEXT NOT NULL CHECK (JSON_VALID(forecast_json)),
            simulation_json LONGTEXT NULL CHECK (simulation_json IS NULL OR JSON_VALID(simulation_json)),
            generation_status VARCHAR(64) NULL,
            no_history_fallback LONGTEXT NULL CHECK (
                no_history_fallback IS NULL OR JSON_VALID(no_history_fallback)
            ),
            simulation_available TINYINT(1) NOT NULL,
            source_epoch VARCHAR(64) NOT NULL,
            source_computed_at DATETIME NULL,
            generated_at DATETIME NOT NULL COMMENT
                'payload.generated_at when present; otherwise source cache updated_at fallback',
            PRIMARY KEY (brand_key, source, market_id),
            CHECK (source IN ('iqvia_nsa', 'ubist')),
            CHECK (
                (simulation_available = 0 AND simulation_json IS NULL) OR
                (simulation_available = 1 AND simulation_json IS NOT NULL)
            ),
            {view_check}
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    horizon = f"""
        CREATE TABLE {HORIZON_TABLE} (
            market_id VARCHAR(64) NOT NULL,
            source VARCHAR(16) NOT NULL,
            measure VARCHAR(32) NOT NULL,
            view_kind VARCHAR(32) NOT NULL,
            forecast_horizon_json LONGTEXT NOT NULL CHECK (JSON_VALID(forecast_horizon_json)),
            source_row_count INT NOT NULL,
            source_epoch VARCHAR(64) NOT NULL,
            source_computed_at DATETIME NULL,
            generated_at DATETIME NOT NULL COMMENT
                'payload.generated_at when present; otherwise source cache updated_at fallback',
            PRIMARY KEY (market_id, source, measure),
            CHECK (source IN ('iqvia_nsa', 'ubist')),
            {view_check}
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    return block, horizon


def _chunks(values: Iterable[Any], size: int = BATCH_SIZE) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _connect(*, stream: bool = False) -> Any:
    cursorclass = pymysql.cursors.SSDictCursor if stream else pymysql.cursors.DictCursor
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3308")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("MYSQL_PWD"),
        database=os.environ.get("MARIADB_DATABASE", DATABASE),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=cursorclass,
        read_timeout=3600,
        write_timeout=3600,
    )


def _assert_database(conn: Any) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
    database = row.get("database_name") if isinstance(row, dict) else row[0]
    if database != DATABASE:
        raise SystemExit(f"refusing non-d2 database: {database!r}")


def _table_count(conn: Any, table: str) -> int:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{table}`")
        row = cursor.fetchone()
    return int(row.get("row_count") if isinstance(row, dict) else row[0])


def _existing_block_keys(conn: Any) -> set[tuple[str, str, str]]:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT brand_key, source, market_id FROM {BLOCK_TABLE}")
        return {(str(row["brand_key"]), str(row["source"]), str(row["market_id"])) for row in cursor}


def _existing_horizon_keys(conn: Any) -> set[tuple[str, str, str]]:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT market_id, source, measure FROM {HORIZON_TABLE}")
        return {(str(row["market_id"]), str(row["source"]), str(row["measure"])) for row in cursor}


def ensure_target_tables(conn: Any) -> None:
    block, horizon = create_table_statements()
    with conn.cursor() as cursor:
        for table, ddl in ((BLOCK_TABLE, block), (HORIZON_TABLE, horizon)):
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
            row = cursor.fetchone()
            exists = int(row.get("n") if isinstance(row, dict) else row[0])
            if not exists:
                cursor.execute(ddl)
    conn.commit()


def migrate_block(read_conn: Any, write_conn: Any) -> dict[str, Any]:
    generated_sources = {"payload.generated_at": 0, "source.updated_at_fallback": 0}
    inserted = 0
    original_rows = 0
    existing = _existing_block_keys(write_conn)
    source_sha = hashlib.sha256()
    sql = f"""
        INSERT INTO {BLOCK_TABLE} (
            brand_key, source, market_id, view_kind, forecast_json, simulation_json,
            generation_status, no_history_fallback, simulation_available, source_epoch,
            source_computed_at, generated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with read_conn.cursor() as reader:
        reader.execute(
            f"SELECT brand_key, atc4_code, response_json, source_computed_at, updated_at "
            f"FROM {SOURCE_BLOCK_TABLE} ORDER BY brand_key, atc4_code"
        )
        for source_batch in _chunks(reader):
            values = []
            for source_row in source_batch:
                original_rows += 1
                source_sha.update(str(source_row["brand_key"]).encode())
                source_sha.update(b"\0")
                source_sha.update(str(source_row["atc4_code"]).encode())
                source_sha.update(b"\0")
                source_sha.update(str(source_row["response_json"]).encode())
                source_sha.update(b"\n")
                rows = split_block_payload(
                    brand_key=str(source_row["brand_key"]),
                    market_id=str(source_row["atc4_code"]),
                    response_json=str(source_row["response_json"]),
                    source_computed_at=source_row["source_computed_at"],
                    updated_at=source_row["updated_at"],
                )
                for row in rows:
                    generated_sources[row.generated_at_source] += 1
                    key = (row.brand_key, row.source, row.market_id)
                    if key in existing:
                        continue
                    values.append(
                        (
                            row.brand_key,
                            row.source,
                            row.market_id,
                            row.view_kind,
                            row.forecast_json,
                            row.simulation_json,
                            row.generation_status,
                            row.no_history_fallback,
                            int(row.simulation_available),
                            row.source_epoch,
                            row.source_computed_at,
                            row.generated_at,
                        )
                    )
            if values:
                with write_conn.cursor() as writer:
                    inserted += writer.executemany(sql, values)
                write_conn.commit()
            print(f"block_progress original={original_rows} inserted={inserted}", flush=True)
    return {
        "original_rows": original_rows,
        "inserted": inserted,
        "generated_at_sources": generated_sources,
        "source_sha256_before": source_sha.hexdigest(),
    }


def migrate_horizon(read_conn: Any, write_conn: Any) -> dict[str, Any]:
    inserted = 0
    source_rows = 0
    existing = _existing_horizon_keys(write_conn)
    source_sha = hashlib.sha256()
    sql = f"""
        INSERT INTO {HORIZON_TABLE} (
            market_id, source, measure, view_kind, forecast_horizon_json, source_row_count,
            source_epoch, source_computed_at, generated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with read_conn.cursor() as reader:
        reader.execute(
            f"SELECT atc4_code, source, measure, market_forecast_json, source_row_count, "
            f"source_computed_at, updated_at FROM {SOURCE_HORIZON_TABLE} "
            "ORDER BY atc4_code, source, measure"
        )
        for source_batch in _chunks(reader, 100):
            values = []
            for row in source_batch:
                source_rows += 1
                source_sha.update(str(row["atc4_code"]).encode())
                source_sha.update(b"\0")
                source_sha.update(str(row["source"]).encode())
                source_sha.update(b"\0")
                source_sha.update(str(row["measure"]).encode())
                source_sha.update(b"\0")
                source_sha.update(str(row["market_forecast_json"]).encode())
                source_sha.update(b"\n")
                key = (str(row["atc4_code"]), str(row["source"]), str(row["measure"]))
                if key in existing:
                    continue
                payload = json.loads(str(row["market_forecast_json"]))
                generated_at, _ = _generated_at(payload, row["updated_at"])
                values.append(
                    (
                        str(row["atc4_code"]),
                        str(row["source"]),
                        str(row["measure"]),
                        derive_view_kind(str(row["atc4_code"])),
                        str(row["market_forecast_json"]),
                        int(row["source_row_count"]),
                        SOURCE_EPOCH,
                        row["source_computed_at"],
                        generated_at,
                    )
                )
            if values:
                with write_conn.cursor() as writer:
                    inserted += writer.executemany(sql, values)
                write_conn.commit()
    return {"source_rows": source_rows, "inserted": inserted, "source_sha256_before": source_sha.hexdigest()}


def validate_loaded(conn: Any) -> dict[str, int]:
    checks: dict[str, int] = {}
    queries = {
        "block_rows": f"SELECT COUNT(*) FROM {BLOCK_TABLE}",
        "block_original_grains": f"SELECT COUNT(*) FROM (SELECT brand_key, market_id FROM {BLOCK_TABLE} GROUP BY brand_key, market_id) x",
        "horizon_rows": f"SELECT COUNT(*) FROM {HORIZON_TABLE}",
        "block_pk_duplicates": f"SELECT COUNT(*) FROM (SELECT brand_key, source, market_id, COUNT(*) n FROM {BLOCK_TABLE} GROUP BY brand_key, source, market_id HAVING n > 1) x",
        "horizon_pk_duplicates": f"SELECT COUNT(*) FROM (SELECT market_id, source, measure, COUNT(*) n FROM {HORIZON_TABLE} GROUP BY market_id, source, measure HAVING n > 1) x",
        "simulation_mismatch": f"SELECT COUNT(*) FROM {BLOCK_TABLE} WHERE (simulation_available=0 AND simulation_json IS NOT NULL) OR (simulation_available=1 AND simulation_json IS NULL)",
        "marker_generation_null": f"SELECT COUNT(*) FROM {BLOCK_TABLE} WHERE generation_status IS NULL",
        "marker_fallback_null": f"SELECT COUNT(*) FROM {BLOCK_TABLE} WHERE no_history_fallback IS NULL",
        "view_kind_mismatch": f"SELECT COUNT(*) FROM {BLOCK_TABLE} WHERE view_kind <> 'general' OR market_id LIKE 'ml\\_%' OR market_id LIKE 'cd\\_%'",
    }
    with conn.cursor() as cursor:
        for name, query in queries.items():
            cursor.execute(query)
            row = cursor.fetchone()
            checks[name] = int(next(iter(row.values())) if isinstance(row, dict) else row[0])
    return checks


def expected_completion_checks() -> dict[str, int]:
    return {
        "block_rows": 35768,
        "block_original_grains": 34378,
        "horizon_rows": 2880,
        "block_pk_duplicates": 0,
        "horizon_pk_duplicates": 0,
        "simulation_mismatch": 0,
        "marker_generation_null": 0,
        "marker_fallback_null": 0,
        "view_kind_mismatch": 0,
    }


def assert_completion(checks: dict[str, int]) -> None:
    expected = expected_completion_checks()
    mismatches = {
        name: {"expected": expected_value, "actual": checks.get(name)}
        for name, expected_value in expected.items()
        if checks.get(name) != expected_value
    }
    if mismatches:
        raise RuntimeError("completion gate failed: " + json.dumps(mismatches, sort_keys=True))


def _block_row_from_database(row: dict[str, Any]) -> BlockRow:
    return BlockRow(
        brand_key=str(row["brand_key"]),
        source=str(row["source"]),
        market_id=str(row["market_id"]),
        view_kind=str(row["view_kind"]),
        forecast_json=str(row["forecast_json"]),
        simulation_json=str(row["simulation_json"]) if row["simulation_json"] is not None else None,
        generation_status=str(row["generation_status"]) if row["generation_status"] is not None else None,
        no_history_fallback=(
            str(row["no_history_fallback"]) if row["no_history_fallback"] is not None else None
        ),
        simulation_available=bool(row["simulation_available"]),
        source_epoch=str(row["source_epoch"]),
        source_computed_at=row["source_computed_at"],
        generated_at=row["generated_at"],
        generated_at_source="stored",
    )


def verify_block_reassembly() -> dict[str, Any]:
    source_conn = _connect(stream=True)
    target_conn = _connect(stream=True)
    exact = 0
    source_sha = hashlib.sha256()
    reassembled_sha = hashlib.sha256()
    try:
        with source_conn.cursor() as source_cursor, target_conn.cursor() as target_cursor:
            source_cursor.execute(
                f"SELECT brand_key, atc4_code, response_json FROM {SOURCE_BLOCK_TABLE} "
                "ORDER BY brand_key, atc4_code"
            )
            target_cursor.execute(
                f"SELECT * FROM {BLOCK_TABLE} ORDER BY brand_key, market_id, source"
            )
            grouped_targets = groupby(
                target_cursor,
                key=lambda row: (str(row["brand_key"]), str(row["market_id"])),
            )
            target_group = next(grouped_targets, None)
            for source_row in source_cursor:
                key = (str(source_row["brand_key"]), str(source_row["atc4_code"]))
                if target_group is None or target_group[0] != key:
                    raise RuntimeError(f"missing target block rows for {key!r}")
                rows = [_block_row_from_database(row) for row in target_group[1]]
                original = str(source_row["response_json"])
                reassembled = reassemble_block_payload(original, rows)
                if reassembled != original:
                    raise RuntimeError(f"block reassembly mismatch for {key!r}")
                for digest, payload in ((source_sha, original), (reassembled_sha, reassembled)):
                    digest.update(key[0].encode())
                    digest.update(b"\0")
                    digest.update(key[1].encode())
                    digest.update(b"\0")
                    digest.update(payload.encode())
                    digest.update(b"\n")
                exact += 1
                target_group = next(grouped_targets, None)
            if target_group is not None:
                raise RuntimeError(f"unexpected target block rows for {target_group[0]!r}")
    finally:
        source_conn.close()
        target_conn.close()
    return {
        "exact": exact,
        "source_sha256_after": source_sha.hexdigest(),
        "reassembled_sha256": reassembled_sha.hexdigest(),
    }


def verify_horizon_reassembly() -> dict[str, Any]:
    source_conn = _connect(stream=True)
    target_conn = _connect(stream=True)
    exact = 0
    source_sha = hashlib.sha256()
    try:
        with source_conn.cursor() as source_cursor, target_conn.cursor() as target_cursor:
            source_cursor.execute(
                f"SELECT atc4_code, source, measure, market_forecast_json, source_row_count, "
                f"source_computed_at FROM {SOURCE_HORIZON_TABLE} "
                "ORDER BY atc4_code, source, measure"
            )
            target_cursor.execute(
                f"SELECT market_id, source, measure, forecast_horizon_json, source_row_count, "
                f"source_computed_at FROM {HORIZON_TABLE} ORDER BY market_id, source, measure"
            )
            for source_row, target_row in zip(source_cursor, target_cursor, strict=True):
                source_key = (
                    str(source_row["atc4_code"]),
                    str(source_row["source"]),
                    str(source_row["measure"]),
                )
                target_key = (
                    str(target_row["market_id"]),
                    str(target_row["source"]),
                    str(target_row["measure"]),
                )
                if source_key != target_key:
                    raise RuntimeError(f"horizon key mismatch: {source_key!r} != {target_key!r}")
                if str(source_row["market_forecast_json"]) != str(target_row["forecast_horizon_json"]):
                    raise RuntimeError(f"horizon payload mismatch for {source_key!r}")
                if int(source_row["source_row_count"]) != int(target_row["source_row_count"]):
                    raise RuntimeError(f"horizon row count mismatch for {source_key!r}")
                if source_row["source_computed_at"] != target_row["source_computed_at"]:
                    raise RuntimeError(f"horizon lineage mismatch for {source_key!r}")
                source_sha.update(source_key[0].encode())
                source_sha.update(b"\0")
                source_sha.update(source_key[1].encode())
                source_sha.update(b"\0")
                source_sha.update(source_key[2].encode())
                source_sha.update(b"\0")
                source_sha.update(str(source_row["market_forecast_json"]).encode())
                source_sha.update(b"\n")
                exact += 1
    finally:
        source_conn.close()
        target_conn.close()
    return {"exact": exact, "source_sha256_after": source_sha.hexdigest()}


def run() -> dict[str, Any]:
    read_conn = _connect(stream=True)
    write_conn = _connect()
    try:
        _assert_database(write_conn)
        if _table_count(write_conn, SOURCE_BLOCK_TABLE) != 34378:
            raise SystemExit("source block count changed")
        if _table_count(write_conn, SOURCE_HORIZON_TABLE) != 2880:
            raise SystemExit("source horizon count changed")
        ensure_target_tables(write_conn)
        block = migrate_block(read_conn, write_conn)
        read_conn.close()
        read_conn = _connect(stream=True)
        horizon = migrate_horizon(read_conn, write_conn)
        checks = validate_loaded(write_conn)
        assert_completion(checks)
        block_verification = verify_block_reassembly()
        horizon_verification = verify_horizon_reassembly()
        if block_verification["exact"] != 34378:
            raise RuntimeError(f"block reassembly incomplete: {block_verification['exact']}")
        if block_verification["source_sha256_after"] != block["source_sha256_before"]:
            raise RuntimeError("source block changed during migration")
        if block_verification["source_sha256_after"] != block_verification["reassembled_sha256"]:
            raise RuntimeError("reassembled block digest differs from source")
        if horizon_verification["exact"] != 2880:
            raise RuntimeError(f"horizon reassembly incomplete: {horizon_verification['exact']}")
        if horizon_verification["source_sha256_after"] != horizon["source_sha256_before"]:
            raise RuntimeError("source horizon changed during migration")
        summary = {
            "block": block,
            "horizon": horizon,
            "checks": checks,
            "block_verification": block_verification,
            "horizon_verification": horizon_verification,
        }
        print("migration_summary=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        return summary
    finally:
        read_conn.close()
        write_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("migration requires --execute")
    run()


if __name__ == "__main__":
    main()
