#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pymysql",
#     "rich",
#     "typer",
# ]
# ///
"""Backfill `brands[].is_jw` in stored topic mart payloads from source company metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sys

import pymysql
import typer
from rich.console import Console


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.auto_topic.audit import write_json  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (  # noqa: E402
    KEYWORD_TABLE,
    SCHEMA,
    connect_mariadb,
    is_jw_representing_company,
    read_env_file,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import validated_stage_schema  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store_db import TOPICS_TABLE  # noqa: E402


CONSOLE = Console()


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    """Evidence for one is_jw payload backfill run."""

    dry_run: bool
    scanned_rows: int
    changed_rows: int
    scanned_brands: int
    true_brand_count: int
    true_brands: tuple[str, ...]


def main(
    stage_schema: str = typer.Option(SCHEMA, "--stage-schema", help="Allowed isolated API schema."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview changes unless --execute is provided."),
    output: Path | None = typer.Option(None, "--output", help="Optional JSON summary path."),
) -> None:
    """Update only `payload.brands[].is_jw` using Keyword-stage representing_company."""
    connection = connect_mariadb(read_env_file())
    try:
        summary = backfill_topic_is_jw(connection, schema=stage_schema, dry_run=dry_run)
    finally:
        connection.close()
    payload = backfill_summary_json(summary)
    if output is not None:
        write_json(output, payload)
    CONSOLE.print_json(data=payload)


def backfill_topic_is_jw(
    connection: pymysql.connections.Connection,
    *,
    schema: str = SCHEMA,
    dry_run: bool = True,
) -> BackfillSummary:
    """Backfill stored topic payload brands from source-company ownership metadata."""
    safe_schema = validated_stage_schema(schema)
    jw_by_brand = load_jw_brand_map(connection, schema=safe_schema)
    changed_rows = 0
    scanned_rows = 0
    scanned_brands: set[str] = set()
    true_brands: set[str] = set()
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT scope_id, payload FROM `{safe_schema}`.`{TOPICS_TABLE}` ORDER BY scope_id")
        rows = cursor.fetchall()
        for row in rows:
            scanned_rows += 1
            scope_id = str(row["scope_id"])
            payload = _json_object(row["payload"])
            updated, changed, row_brands, row_true_brands = mark_payload_is_jw(payload, jw_by_brand)
            scanned_brands.update(row_brands)
            true_brands.update(row_true_brands)
            if not changed:
                continue
            changed_rows += 1
            if not dry_run:
                cursor.execute(
                    f"UPDATE `{safe_schema}`.`{TOPICS_TABLE}` SET payload=%s WHERE scope_id=%s",
                    (json.dumps(updated, ensure_ascii=False, sort_keys=True), scope_id),
                )
    if not dry_run:
        connection.commit()
    return BackfillSummary(
        dry_run=dry_run,
        scanned_rows=scanned_rows,
        changed_rows=changed_rows,
        scanned_brands=len(scanned_brands),
        true_brand_count=len(true_brands),
        true_brands=tuple(sorted(true_brands)),
    )


def load_jw_brand_map(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> dict[str, bool]:
    """Return product_name -> is_jw based on observed source representing_company values."""
    safe_schema = validated_stage_schema(schema)
    result: dict[str, bool] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT product_name, representing_company
            FROM `{safe_schema}`.`{KEYWORD_TABLE}`
            WHERE product_name IS NOT NULL AND product_name <> ''
            """
        )
        for row in cursor.fetchall():
            brand = str(row["product_name"])
            current = result.get(brand, False)
            result[brand] = current or is_jw_representing_company(str(row["representing_company"] or ""))
    return result


def mark_payload_is_jw(
    payload: dict[str, JsonValue],
    jw_by_brand: dict[str, bool],
) -> tuple[dict[str, JsonValue], bool, set[str], set[str]]:
    """Return payload with only `brands[].is_jw` adjusted."""
    changed = False
    brands_seen: set[str] = set()
    true_brands: set[str] = set()
    brands = payload.get("brands")
    if not isinstance(brands, list):
        return payload, False, brands_seen, true_brands
    updated_brands: list[JsonValue] = []
    for value in brands:
        if not isinstance(value, dict):
            updated_brands.append(value)
            continue
        brand = str(value.get("brand") or "")
        brands_seen.add(brand)
        is_jw = jw_by_brand.get(brand, False)
        if is_jw:
            true_brands.add(brand)
        if bool(value.get("is_jw")) != is_jw:
            changed = True
        updated_brands.append({**value, "is_jw": is_jw})
    if not changed:
        return payload, False, brands_seen, true_brands
    return {**payload, "brands": updated_brands}, True, brands_seen, true_brands


def backfill_summary_json(summary: BackfillSummary) -> dict[str, JsonValue]:
    """Serialize a backfill summary without sensitive data."""
    return {
        "dry_run": summary.dry_run,
        "scanned_rows": summary.scanned_rows,
        "changed_rows": summary.changed_rows,
        "scanned_brands": summary.scanned_brands,
        "true_brand_count": summary.true_brand_count,
        "true_brands": list(summary.true_brands),
    }


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    typer.run(main)
