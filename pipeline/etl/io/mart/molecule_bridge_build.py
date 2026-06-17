"""Build ``mart_brand_molecule`` from mart and catalog molecule evidence.

The builder writes only the additive bridge table.  It intentionally refuses to
write the operating ``jw_mart`` schema unless an explicit environment override
is set, because the normal workflow is isolated build/validation before a
separate operations gate.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Final
import json
import os

import pymysql

from pipeline.etl.io.mart.general_config import PROJECT_ROOT, load_env
from pipeline.etl.io.mart.molecule_bridge_schema import (
    BRIDGE_INSERT_COLUMNS,
    MART_BRAND_MOLECULE_DDL,
    BridgeBuildStats,
    BridgeInsertPayload,
    MoleculeBridgeRecord,
    bridge_record_key,
)
from pipeline.etl.io.mart.molecule_bridge_sources import (
    iter_catalog_records,
    iter_general_dimension_records,
    iter_strategic_overlay_records,
)
from pipeline.etl.lib.ops_utils import first_existing


PROTECTED_OPERATING_DBS: Final[frozenset[str]] = frozenset({"jw_mart"})
STRATEGIC_SOURCES: Final[tuple[tuple[str, str], ...]] = (
    ("mart_strategic_ml_brand_metric", "strategic_ml_overlay"),
    ("mart_strategic_cd_brand_metric", "strategic_cd_overlay"),
)


def _db_env() -> dict[str, str]:
    """Load MariaDB credentials without printing any secret value."""

    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    for key, value in os.environ.items():
        if key.startswith("MARIADB_") or key == "HOST_PORT":
            env[key] = value
    return env


def _connect(database: str | None = None) -> pymysql.connections.Connection:
    """Open an autocommit MariaDB connection for DDL and bridge inserts."""

    env = _db_env()
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_PASSWORD")
    user = "root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    return pymysql.connect(
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3307")),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _guard_target_db(target_db: str) -> None:
    """Prevent accidental writes to the operating mart schema."""

    if target_db in PROTECTED_OPERATING_DBS and os.environ.get("ALLOW_OPERATING_MOLECULE_BRIDGE") != "1":
        raise RuntimeError(
            "refusing to build mart_brand_molecule in jw_mart; "
            "use an isolated --target-db or set ALLOW_OPERATING_MOLECULE_BRIDGE=1 in an operations-gated run"
        )


def _create_bridge_table(conn: pymysql.connections.Connection, target_db: str) -> None:
    """Create bridge table from the embedded DDL in the requested schema."""

    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
        cur.execute(f"DROP TABLE IF EXISTS `{target_db}`.mart_brand_molecule")
        cur.execute(f"USE `{target_db}`")
        cur.execute(MART_BRAND_MOLECULE_DDL)


def _collect_records(
    conn: pymysql.connections.Connection,
    source_db: str,
    catalog_root: Path,
    max_rows: int | None,
) -> list[MoleculeBridgeRecord]:
    """Collect all bridge evidence rows before final source consolidation."""

    records = list(iter_general_dimension_records(conn, source_db, max_rows=max_rows))
    for table_name, evidence_scope in STRATEGIC_SOURCES:
        records.extend(
            iter_strategic_overlay_records(conn, source_db, table_name, evidence_scope, max_rows=max_rows)
        )
    records.extend(iter_catalog_records(catalog_root, max_rows=max_rows))
    return records


def _payloads(records: list[MoleculeBridgeRecord]) -> list[BridgeInsertPayload]:
    """Consolidate candidate rows into table-unique insert payloads."""

    grouped: dict[tuple[str, str, str, str], list[MoleculeBridgeRecord]] = defaultdict(list)
    for record in records:
        grouped[bridge_record_key(record)].append(record)

    payloads: list[BridgeInsertPayload] = []
    for key in sorted(grouped):
        members = grouped[key]
        first = members[0]
        raw_examples = sorted({member.molecule_raw for member in members})[:8]
        scopes = sorted({member.evidence_scope for member in members})
        payloads.append(
            {
                "brand_key": first.brand_key,
                "brand_name": first.brand_name,
                "atc4_code": first.atc4_code,
                "mart_source": first.mart_source,
                "molecule_norm": first.molecule_norm,
                "molecule_display": first.molecule_display,
                "molecule_raw_examples": json.dumps(raw_examples, ensure_ascii=False),
                "evidence_scopes": json.dumps(scopes, ensure_ascii=False),
                "evidence_count": len(members),
                "component_count": max(member.component_count for member in members),
                "is_combo_component": int(any(member.is_combo_component for member in members)),
            }
        )
    return payloads


def _insert_payloads(conn: pymysql.connections.Connection, target_db: str, payloads: list[BridgeInsertPayload]) -> None:
    """Insert bridge rows using one deterministic column list."""

    if not payloads:
        return
    placeholders = ",".join(["%s"] * len(BRIDGE_INSERT_COLUMNS))
    columns = ",".join(BRIDGE_INSERT_COLUMNS)
    sql = f"INSERT INTO `{target_db}`.mart_brand_molecule ({columns}) VALUES ({placeholders})"
    values = [tuple(payload[column] for column in BRIDGE_INSERT_COLUMNS) for payload in payloads]
    with conn.cursor() as cur:
        cur.executemany(sql, values)


def build_molecule_bridge(
    *,
    source_db: str,
    target_db: str,
    catalog_root: Path,
    max_rows: int | None = None,
) -> BridgeBuildStats:
    """Rebuild ``mart_brand_molecule`` from source mart/catalog evidence."""

    _guard_target_db(target_db)
    conn = _connect()
    try:
        _create_bridge_table(conn, target_db)
        records = _collect_records(conn, source_db, catalog_root, max_rows)
        payloads = _payloads(records)
        _insert_payloads(conn, target_db, payloads)
    finally:
        conn.close()

    return BridgeBuildStats(
        target_db=target_db,
        source_db=source_db,
        inserted_rows=len(payloads),
        candidate_rows=len(records),
        brand_keys=len({payload["brand_key"] for payload in payloads}),
        molecule_norms=len({payload["molecule_norm"] for payload in payloads}),
        combo_rows=sum(payload["is_combo_component"] for payload in payloads),
    )
