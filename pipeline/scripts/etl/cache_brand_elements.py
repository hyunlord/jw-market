from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pymysql

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents, Path("/app"), Path("/workspace")):
    if (candidate / "pipeline").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from cache_build_common import mariadb_connect
from pipeline.scripts.etl.cache_deep_analysis_brand_factors import (
    LOAD_BATCH_SIZE,
    dump_brand_factors,
    empty_brand_factors,
    load_brand_factor_map,
    quote_ident,
)
from pipeline.scripts.utils.mart_config import DEFAULT_MART_DB_NAME
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


TARGET_DATABASE: Final[str] = DEFAULT_MART_DB_NAME
CACHE_TABLE: Final[str] = "cache_brand_elements"
AGENT3_TABLE: Final[str] = "agent3_brand_strength"
DEFAULT_SOURCE_TABLES: Final[tuple[str, ...]] = ("cache_deep_analysis", "cache_deep_analysis_general")
DEFAULT_BRAND_ELEMENTS_TTL_DAYS: Final[int] = 35
REHEARSAL_CACHE_PREFIX: Final[str] = "jw_mart_s6_rehearsal_"


class CacheBrandElementsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrandElementPayload:
    brand_key: str
    brand_name: str
    factors: dict[str, Any]
    strength: dict[str, Any]
    strength_generated_at: Any
    strength_workflow_rev: Any
    source_computed_at: Any | None
    expires_at: Any | None


def dump_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def not_generated_strength() -> dict[str, Any]:
    return {"available": False, "reason": "not_generated"}


def parse_strength_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return not_generated_strength()
    try:
        summary = json.loads(row.get("strength_summary_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return not_generated_strength()
    if not isinstance(summary, dict):
        return not_generated_strength()
    return {
        "available": True,
        "profile_display": summary.get("profile_display"),
        "strength_items": summary.get("strength_items", []),
        "limitations": summary.get("limitations", []),
        "meta": {
            "generated_at": _format_generated_at(row.get("generated_at")),
            "workflow_rev": row.get("workflow_rev"),
        },
    }


def _format_generated_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def brand_elements_ttl_days() -> int:
    raw_value = os.environ.get("BRAND_ELEMENTS_CACHE_TTL_DAYS", str(DEFAULT_BRAND_ELEMENTS_TTL_DAYS))
    try:
        return max(1, int(raw_value))
    except ValueError:
        return DEFAULT_BRAND_ELEMENTS_TTL_DAYS


def cache_expires_at(ttl_days: int | None = None) -> datetime:
    return datetime.now() + timedelta(days=ttl_days or brand_elements_ttl_days())


def _table_columns(conn: Any, table_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {quote_ident(table_name)}")
        return {str(row.get("Field") or row.get("field") or "") for row in cur.fetchall()}


def _table_exists(conn: Any, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS table_exists
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
            """,
            (table_name,),
        )
        try:
            row = cur.fetchone()
        except IndexError:
            return False
    return bool(row and int(row.get("table_exists") or 0))


def _ensure_columns(conn: Any, table_name: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table_name)
    table = quote_ident(table_name)
    with conn.cursor() as cur:
        for column, ddl in columns.items():
            if column not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        if "expires_at" not in existing:
            cur.execute(f"CREATE INDEX idx_cache_brand_elements_expires ON {table} (expires_at)")


def ensure_cache_brand_elements_table(conn: Any, table_name: str = CACHE_TABLE) -> None:
    if _table_exists(conn, table_name):
        return
    table = quote_ident(table_name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                brand_key VARCHAR(255) NOT NULL,
                brand_name VARCHAR(255) NOT NULL,
                brand_name_compact VARCHAR(255) NOT NULL,
                factors_json LONGTEXT NOT NULL CHECK (JSON_VALID(factors_json)),
                strength_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_json)),
                strength_generated_at DATETIME NULL,
                strength_workflow_rev VARCHAR(64) NULL,
                source_computed_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand_key),
                KEY idx_cache_brand_elements_compact (brand_name_compact),
                KEY idx_cache_brand_elements_updated_at (updated_at),
                KEY idx_cache_brand_elements_expires (expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
    _ensure_columns(
        conn,
        table_name,
        {
            "source_computed_at": "source_computed_at TIMESTAMP NULL",
            "expires_at": "expires_at TIMESTAMP NULL",
        },
    )


def source_brands(conn: Any, source_tables: Sequence[str] = DEFAULT_SOURCE_TABLES, limit: int | None = None) -> list[str]:
    brands: set[str] = set()
    for table in source_tables:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT brand FROM {quote_ident(table)} WHERE NULLIF(brand, '') IS NOT NULL")
                brands.update(str(row["brand"]) for row in cur.fetchall() if row.get("brand"))
        except pymysql.err.ProgrammingError as exc:
            if exc.args and exc.args[0] == 1146:
                continue
            raise
    ordered = sorted(brands)
    return ordered[:limit] if limit else ordered


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"


def load_strength_map(conn: Any, brands: Sequence[str], agent3_schema: str) -> dict[str, Mapping[str, Any]]:
    if not brands:
        return {}
    exact_rows: dict[str, Mapping[str, Any]] = {}
    compact_to_brand: dict[str, str] = {}
    ambiguous_compact: set[str] = set()
    for brand in brands:
        compact = compact_brand_name(brand)
        if not compact:
            continue
        previous = compact_to_brand.get(compact)
        if previous is None:
            compact_to_brand[compact] = brand
        elif previous != brand:
            ambiguous_compact.add(compact)

    for start in range(0, len(brands), LOAD_BATCH_SIZE):
        batch = list(brands[start : start + LOAD_BATCH_SIZE])
        placeholders = ", ".join(["%s"] * len(batch))
        compact_batch = [compact_brand_name(brand) for brand in batch if compact_brand_name(brand)]
        compact_placeholders = ", ".join(["%s"] * len(compact_batch))
        compact_filter = f" OR {_compact_sql('serving_brand_name')} IN ({compact_placeholders})" if compact_batch else ""
        params: tuple[str, ...] = tuple(batch) + tuple(compact_batch)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT serving_brand_name, strength_summary_json, generated_at, workflow_rev
                FROM {quote_ident(agent3_schema)}.{quote_ident(AGENT3_TABLE)}
                WHERE serving_brand_name IN ({placeholders}){compact_filter}
                """,
                params,
            )
            for row in cur.fetchall():
                serving_brand = str(row.get("serving_brand_name") or "")
                if serving_brand in batch:
                    exact_rows[serving_brand] = row
                    continue
                compact = compact_brand_name(serving_brand)
                if compact and compact not in ambiguous_compact:
                    target = compact_to_brand.get(compact)
                    if target and target not in exact_rows:
                        exact_rows[target] = row
    return exact_rows


def load_source_computed_at_map(conn: Any, brands: Sequence[str]) -> dict[str, Any]:
    if not brands:
        return {}
    source_by_brand: dict[str, Any] = {}
    for start in range(0, len(brands), LOAD_BATCH_SIZE):
        batch = list(brands[start : start + LOAD_BATCH_SIZE])
        placeholders = ", ".join(["%s"] * len(batch))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT brand_key, brand_name, MAX(computed_at) AS source_computed_at
                FROM `mart_general_brand_metric`
                WHERE brand_key IN ({placeholders}) OR brand_name IN ({placeholders})
                GROUP BY brand_key, brand_name
                """,
                tuple(batch) + tuple(batch),
            )
            for row in cur.fetchall():
                value = row.get("source_computed_at")
                for key in (row.get("brand_key"), row.get("brand_name")):
                    if not key:
                        continue
                    brand_key = str(key)
                    existing = source_by_brand.get(brand_key)
                    if brand_key not in source_by_brand or existing is None or (value is not None and value > existing):
                        source_by_brand[brand_key] = value
    return source_by_brand


def build_brand_element_payloads(conn: Any, brands: Sequence[str], *, agent3_schema: str) -> list[BrandElementPayload]:
    factors_by_brand = load_brand_factor_map(conn, brands)
    strength_by_brand = load_strength_map(conn, brands, agent3_schema)
    source_computed_at_by_brand = load_source_computed_at_map(conn, brands)
    expires_at = cache_expires_at()
    payloads: list[BrandElementPayload] = []
    for brand in brands:
        strength_row = strength_by_brand.get(brand)
        payloads.append(
            BrandElementPayload(
                brand_key=brand,
                brand_name=brand,
                factors=factors_by_brand.get(brand) or empty_brand_factors(),
                strength=parse_strength_row(strength_row),
                strength_generated_at=strength_row.get("generated_at") if strength_row else None,
                strength_workflow_rev=strength_row.get("workflow_rev") if strength_row else None,
                source_computed_at=source_computed_at_by_brand.get(brand),
                expires_at=expires_at,
            )
        )
    return payloads


def upsert_brand_elements(conn: Any, payloads: Sequence[BrandElementPayload], table_name: str = CACHE_TABLE) -> int:
    if not payloads:
        return 0
    sql = f"""
        INSERT INTO {quote_ident(table_name)}
            (brand_key, brand_name, brand_name_compact, factors_json, strength_json,
             strength_generated_at, strength_workflow_rev, source_computed_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            brand_name = VALUES(brand_name),
            brand_name_compact = VALUES(brand_name_compact),
            factors_json = VALUES(factors_json),
            strength_json = VALUES(strength_json),
            strength_generated_at = VALUES(strength_generated_at),
            strength_workflow_rev = VALUES(strength_workflow_rev),
            source_computed_at = VALUES(source_computed_at),
            expires_at = VALUES(expires_at)
    """
    rows = [
        (
            item.brand_key,
            item.brand_name,
            compact_brand_name(item.brand_name),
            dump_brand_factors(item.factors),
            dump_json(item.strength),
            item.strength_generated_at,
            item.strength_workflow_rev,
            item.source_computed_at,
            item.expires_at,
        )
        for item in payloads
    ]
    with conn.cursor() as cur:
        for start in range(0, len(rows), LOAD_BATCH_SIZE):
            cur.executemany(sql, rows[start : start + LOAD_BATCH_SIZE])
    conn.commit()
    return len(rows)


def verify_cache_brand_elements(conn: Any, table_name: str = CACHE_TABLE) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS rows_total,
                   SUM(JSON_VALID(factors_json)) AS factors_json_valid,
                   SUM(JSON_VALID(strength_json)) AS strength_json_valid,
                   SUM(JSON_EXTRACT(strength_json, '$.available') = true) AS rows_with_strength,
                   SUM(JSON_LENGTH(factors_json, '$.atc') > 0) AS rows_with_atc,
                   SUM(expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP) AS expired_rows
            FROM {quote_ident(table_name)}
            """
        )
        return dict(cur.fetchone())


def connect_db() -> Any:
    database = os.environ.get("MARIADB_DATABASE", TARGET_DATABASE)
    if database != TARGET_DATABASE and not database.startswith(REHEARSAL_CACHE_PREFIX):
        raise CacheBrandElementsError(f"refusing to write non-d2 database: {database}")
    user = os.environ.get("D2_WRITER_USER") or os.environ.get("MARIADB_USER")
    password = os.environ.get("D2_WRITER_PASSWORD") or os.environ.get("MARIADB_PASSWORD")
    if user and password and os.environ.get("MARIADB_HOST"):
        return pymysql.connect(
            host=os.environ["MARIADB_HOST"],
            port=int(os.environ.get("MARIADB_PORT", "3306")),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
    return mariadb_connect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cache_brand_elements from mart factors and Agent3 strength rows.")
    parser.add_argument("--table", default=CACHE_TABLE)
    parser.add_argument("--agent3-schema", default=os.environ.get("AGENT3_DB_NAME", "agent3"))
    parser.add_argument("--ensure-table", action="store_true")
    parser.add_argument("--pilot-fill", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--brand", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not any((args.ensure_table, args.pilot_fill, args.verify, args.dry_run)):
        raise CacheBrandElementsError("at least one action is required")
    conn = connect_db()
    result: dict[str, Any] = {"database": os.environ.get("MARIADB_DATABASE", TARGET_DATABASE), "table": args.table}
    try:
        # --dry-run wins over every other flag: no DDL, no upsert, no commit,
        # regardless of combination (2026-07-17 incident: --dry-run --pilot-fill
        # combined still upserted one live row).
        if args.dry_run:
            blocked = [name for name in ("ensure_table", "pilot_fill") if getattr(args, name)]
            if blocked:
                result["dry_run_blocked_writes"] = blocked
        if args.ensure_table and not args.dry_run:
            ensure_cache_brand_elements_table(conn, args.table)
            conn.commit()
            result["ensure_table"] = {"ok": True}
        brands = list(dict.fromkeys(args.brand)) if args.brand else []
        if not brands and (args.pilot_fill or args.dry_run):
            brands = source_brands(conn, limit=args.limit)
        if args.dry_run:
            payloads = build_brand_element_payloads(conn, brands, agent3_schema=args.agent3_schema)
            result["dry_run"] = {"brands": len(brands), "sample": [payload.brand_key for payload in payloads[:10]]}
        if args.pilot_fill and not args.dry_run:
            ensure_cache_brand_elements_table(conn, args.table)
            payloads = build_brand_element_payloads(conn, brands, agent3_schema=args.agent3_schema)
            result["pilot_fill"] = {"upserted_rows": upsert_brand_elements(conn, payloads, args.table)}
        if args.verify:
            result["verify"] = verify_cache_brand_elements(conn, args.table)
        print("CACHE_BRAND_ELEMENTS_JSON=" + json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
