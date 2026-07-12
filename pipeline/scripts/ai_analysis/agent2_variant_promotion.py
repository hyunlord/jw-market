from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import pymysql

from agent2_variant_contract import VariantLineage, parse_legacy_lineage, validate_variant_payload


LIVE_TABLE = "cache_deep_analysis_ai_analysis"
LINEAGE_COLUMNS = (
    "workflow_id",
    "workflow_revision_id",
    "generation_id",
    "input_hash",
    "generated_at",
    "source_epoch",
    "generation_status",
)
_TABLE_NAME = re.compile(r"^[a-zA-Z0-9_]+$")


@dataclass(frozen=True)
class VariantRecord:
    payload: Mapping[str, Any]
    lineage: VariantLineage


@dataclass(frozen=True)
class PromotionRow:
    brand: str
    brand_key: str
    market_id: str | None
    short: VariantRecord
    long: VariantRecord


def lineage_column_definitions() -> dict[str, str]:
    definitions = {"brand_key": "VARCHAR(255) NULL AFTER brand"}
    for variant in ("short", "long"):
        definitions.update(
            {
                f"{variant}_workflow_id": "INT NULL",
                f"{variant}_workflow_revision_id": "INT NULL",
                f"{variant}_generation_id": "VARCHAR(255) NULL",
                f"{variant}_input_hash": "CHAR(64) NULL",
                f"{variant}_generated_at": "DATETIME(6) NULL",
                f"{variant}_source_epoch": "VARCHAR(255) NULL",
                f"{variant}_generation_status": "VARCHAR(32) NULL",
            }
        )
    return definitions


def additive_schema_sql(existing_columns: set[str], table: str = LIVE_TABLE) -> list[str]:
    _validate_table_name(table)
    return [
        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        for name, definition in lineage_column_definitions().items()
        if name not in existing_columns
    ]


def should_skip(existing_hash: str | None, existing_status: str | None, incoming: VariantRecord) -> bool:
    return (
        existing_status == "complete"
        and existing_hash is not None
        and existing_hash == incoming.lineage.input_hash
    )


def validate_promotion_row(row: PromotionRow) -> None:
    if not row.brand or not row.brand_key:
        raise ValueError("brand and brand_key are required")
    validate_variant_payload(row.short.payload, "short")
    validate_variant_payload(row.long.payload, "long")
    if row.short.lineage.generation_status != "complete":
        raise ValueError("short lineage must be complete")
    if row.long.lineage.generation_status != "complete":
        raise ValueError("long lineage must be complete")


def promotion_values(row: PromotionRow) -> tuple[Any, ...]:
    validate_promotion_row(row)
    values: list[Any] = [
        row.brand,
        row.brand_key,
        row.market_id,
        json.dumps(row.short.payload, ensure_ascii=False),
        json.dumps(row.long.payload, ensure_ascii=False),
    ]
    for record in (row.short, row.long):
        lineage = record.lineage
        values.extend(
            [
                lineage.workflow_id,
                lineage.workflow_revision_id,
                lineage.generation_id,
                lineage.input_hash,
                lineage.generated_at,
                lineage.source_epoch,
                lineage.generation_status,
            ]
        )
    return tuple(values)


def promotion_insert_sql(table: str) -> str:
    _validate_table_name(table)
    columns = ["brand", "brand_key", "market_id", "ai_analysis_short_json", "ai_analysis_long_json"]
    for variant in ("short", "long"):
        columns.extend(f"{variant}_{suffix}" for suffix in LINEAGE_COLUMNS)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = [
        "brand_key = VALUES(brand_key)",
        "market_id = COALESCE(VALUES(market_id), market_id)",
        "ai_analysis_short_json = VALUES(ai_analysis_short_json)",
        "ai_analysis_long_json = VALUES(ai_analysis_long_json)",
    ]
    for variant in ("short", "long"):
        updates.extend(f"{variant}_{suffix} = VALUES({variant}_{suffix})" for suffix in LINEAGE_COLUMNS)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {', '.join(updates)}"
    )


def assert_completion(counts: Mapping[str, int], expected: int) -> None:
    required = ("route_count", "short_complete", "long_complete", "inserted")
    mismatches = {name: counts.get(name) for name in required if counts.get(name) != expected}
    if mismatches:
        raise RuntimeError(f"completion gate failed: expected={expected}, actual={mismatches}")


def atomic_swap_sql(live: str, candidate: str, backup: str) -> str:
    for table in (live, candidate, backup):
        _validate_table_name(table)
    return f"RENAME TABLE {live} TO {backup}, {candidate} TO {live}"


def execute_rows(conn: Any, table: str, rows: Sequence[PromotionRow]) -> int:
    """Persist payload and lineage in the same statement and transaction."""

    sql = promotion_insert_sql(table)
    values = [promotion_values(row) for row in rows]
    with conn.cursor() as cursor:
        cursor.executemany(sql, values)
    conn.commit()
    return len(values)


def _validate_table_name(table: str) -> None:
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError(f"unsafe table name: {table!r}")


def connect_from_env(args: argparse.Namespace) -> Any:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=os.environ.get("DB_PASSWORD", args.db_password),
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def apply_additive_schema(conn: Any, table: str = LIVE_TABLE) -> list[str]:
    _validate_table_name(table)
    with conn.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM {table}")
        existing = {str(row["Field"]) for row in cursor.fetchall()}
        statements = additive_schema_sql(existing, table)
        for statement in statements:
            cursor.execute(statement)
    conn.commit()
    return statements


def backfill_legacy_lineage(conn: Any, table: str = LIVE_TABLE, batch_size: int = 500) -> dict[str, int]:
    """Bind only lineage recoverable from existing JSON payloads."""

    _validate_table_name(table)
    totals = {"rows": 0, "short": 0, "long": 0, "invalid": 0}
    last_brand = ""
    while True:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT brand, ai_analysis_short_json, ai_analysis_long_json FROM {table} "
                "WHERE brand > %s ORDER BY brand LIMIT %s",
                (last_brand, batch_size),
            )
            rows = list(cursor.fetchall())
        if not rows:
            break
        with conn.cursor() as cursor:
            for row in rows:
                values: list[Any] = [row["brand"]]
                assignments = ["brand_key = COALESCE(brand_key, %s)"]
                for variant in ("short", "long"):
                    lineage = parse_legacy_lineage(row.get(f"ai_analysis_{variant}_json"))
                    if lineage is None:
                        continue
                    assignments.extend(
                        [
                            f"{variant}_generation_id = COALESCE({variant}_generation_id, %s)",
                            f"{variant}_generated_at = COALESCE({variant}_generated_at, %s)",
                            f"{variant}_generation_status = COALESCE({variant}_generation_status, %s)",
                        ]
                    )
                    values.extend([lineage.generation_id, lineage.generated_at, lineage.generation_status])
                    totals[variant] += 1
                    totals["invalid"] += int(lineage.generation_status == "invalid")
                values.append(row["brand"])
                cursor.execute(f"UPDATE {table} SET {', '.join(assignments)} WHERE brand = %s", values)
                totals["rows"] += 1
        conn.commit()
        last_brand = str(rows[-1]["brand"])
    return totals


def prepare_candidate(conn: Any, candidate: str, live: str = LIVE_TABLE) -> None:
    _validate_table_name(candidate)
    _validate_table_name(live)
    with conn.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {candidate}")
        cursor.execute(f"CREATE TABLE {candidate} LIKE {live}")
        cursor.execute(f"INSERT INTO {candidate} SELECT * FROM {live}")
    conn.commit()


def _lineage_from_json(value: Mapping[str, Any]) -> VariantLineage:
    generated_at = value.get("generated_at")
    if isinstance(generated_at, str):
        generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    return VariantLineage(
        workflow_id=value.get("workflow_id"),
        workflow_revision_id=value.get("workflow_revision_id"),
        generation_id=value.get("generation_id"),
        input_hash=value.get("input_hash"),
        generated_at=generated_at,
        source_epoch=value.get("source_epoch"),
        generation_status=str(value.get("generation_status")),
        deterministic=bool(value.get("deterministic", False)),
    )


def iter_rows(path: Path) -> Iterator[PromotionRow]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                yield PromotionRow(
                    brand=str(value["brand"]),
                    brand_key=str(value["brand_key"]),
                    market_id=value.get("market_id"),
                    short=VariantRecord(value["short"]["payload"], _lineage_from_json(value["short"]["lineage"])),
                    long=VariantRecord(value["long"]["payload"], _lineage_from_json(value["long"]["lineage"])),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid promotion row at line {line_number}: {exc}") from exc


def load_rows(path: Path) -> list[PromotionRow]:
    return list(iter_rows(path))


def execute_row_batches(conn: Any, table: str, rows: Iterable[PromotionRow], batch_size: int = 250) -> int:
    batch: list[PromotionRow] = []
    loaded = 0
    for row in rows:
        batch.append(row)
        if len(batch) == batch_size:
            loaded += execute_rows(conn, table, batch)
            batch.clear()
    if batch:
        loaded += execute_rows(conn, table, batch)
    return loaded


def candidate_counts(conn: Any, table: str) -> dict[str, int]:
    _validate_table_name(table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS inserted, "
            "SUM(ai_analysis_short_json IS NOT NULL AND short_generation_status = 'complete') AS short_complete, "
            "SUM(ai_analysis_long_json IS NOT NULL AND long_generation_status = 'complete') AS long_complete "
            f"FROM {table}"
        )
        row = cursor.fetchone()
    return {name: int(row.get(name) or 0) for name in ("inserted", "short_complete", "long_complete")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Promote Agent2 short/long variants with lineage")
    parser.add_argument("command", choices=("migrate", "backfill", "prepare", "load", "verify", "swap"))
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-name", default="jw_mart_d2_stage_20260630_r2")
    parser.add_argument("--live", default=LIVE_TABLE)
    parser.add_argument("--candidate", default="cache_deep_analysis_ai_analysis_new_short_long_20260712")
    parser.add_argument("--backup", default="cache_deep_analysis_ai_analysis_bak_pre_short_long_20260712")
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--expected", type=int, default=24789)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    conn = connect_from_env(args)
    try:
        if args.command == "migrate":
            result: Any = {"statements": apply_additive_schema(conn, args.live)}
        elif args.command == "backfill":
            result = backfill_legacy_lineage(conn, args.live)
        elif args.command == "prepare":
            prepare_candidate(conn, args.candidate, args.live)
            result = {"candidate": args.candidate}
        elif args.command == "load":
            if args.jsonl is None:
                raise ValueError("--jsonl is required for load")
            result = {"loaded": execute_row_batches(conn, args.candidate, iter_rows(args.jsonl))}
        elif args.command == "verify":
            counts = candidate_counts(conn, args.candidate)
            counts["route_count"] = args.expected
            assert_completion(counts, args.expected)
            result = counts
        else:
            counts = candidate_counts(conn, args.candidate)
            counts["route_count"] = args.expected
            assert_completion(counts, args.expected)
            with conn.cursor() as cursor:
                cursor.execute(atomic_swap_sql(args.live, args.candidate, args.backup))
            conn.commit()
            result = {"swapped": True, **counts}
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
