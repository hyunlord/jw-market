from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import gzip
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Final, Mapping

import pandas as pd
import pymysql

from pipeline.etl.io.mart.general_config import PROJECT_ROOT, load_env
from pipeline.etl.io.mart.general_json import dumps
from pipeline.etl.io.mart.layer3_compute_market_metric import compute_market_mart_payload
from pipeline.etl.io.mart.molecule_bridge import build_molecule_bridge
from pipeline.etl.io.mart.strategic_constants import ML_MARKET_COLUMNS
from pipeline.etl.lib.ops_utils import first_existing
from pipeline.etl.stages import s4_mart, s5_mart
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_exists


MART_TABLES = (
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_brand_molecule",
)
STRATEGIC_MARKET_TABLE = "mart_strategic_ml_market_metric"
STRATEGIC_BRAND_TABLE = "mart_strategic_ml_brand_metric"
PROTECTED_TARGETS = frozenset({"jw_mart"})
SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")
COUNT_READBACK_RETRY_LIMIT: Final = 5
COUNT_READBACK_RETRY_DELAY_SECONDS: Final = 1.0
STRATEGIC_BRAND_REQUIRED_COLUMNS = (
    "ml_id",
    "brand_id",
    "brand_key",
    "brand_name",
    "source",
    "measure",
    "unit_label",
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "dimension_data",
    "dimension_channel_data",
    "by_dimension",
    "raw_value_history",
    "overlay_data",
    "payload",
)
STRATEGIC_BRAND_OPTIONAL_JSON_COLUMNS = (
    "dimension_specialty_data",
    "channel_specialty_matrix",
    "ubist_channel_by_display",
    "ubist_channel_by_code",
)
STRATEGIC_BRAND_JSON_COLUMNS = frozenset(
    {
        "metric_history",
        "extended_metric_history",
        "channel_data",
        "specialty_data",
        "dimension_data",
        "dimension_channel_data",
        "dimension_specialty_data",
        "by_dimension",
        "raw_value_history",
        "overlay_data",
        "payload",
        "channel_specialty_matrix",
        "ubist_channel_by_display",
        "ubist_channel_by_code",
    }
)


@dataclass(frozen=True, slots=True)
class PublishAction:
    table: str
    mode: str
    target_table: str
    backup_table: str | None
    row_count: int


@dataclass(frozen=True, slots=True)
class DumpResult:
    path: Path
    size_bytes: int
    elapsed_seconds: float


def load_env_file(path: Path | None) -> None:
    if path is None:
        return
    if not path.exists():
        raise FileNotFoundError(f"--env-file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def connect_admin() -> pymysql.connections.Connection:
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
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def db_endpoint_summary() -> dict[str, str]:
    env = _db_env()
    user = "root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp")
    return {
        "host": env.get("MARIADB_HOST", "127.0.0.1"),
        "port": env.get("MARIADB_PORT") or env.get("HOST_PORT", "3307"),
        "user": user,
    }


def validate_schema_name(label: str, db_name: str) -> None:
    if not SCHEMA_RE.fullmatch(db_name):
        raise ValueError(f"{label} must contain only letters, numbers, and underscores: {db_name!r}")
    blocked = {"mysql", "information_schema", "performance_schema", "sys"}
    if db_name.lower() in blocked:
        raise ValueError(f"{label} points at a system schema: {db_name}")


def guard_run(*, source_db: str, target_db: str, build_db: str, allow_operating_target: bool) -> None:
    for label, db_name in (("source_db", source_db), ("target_db", target_db), ("build_db", build_db)):
        validate_schema_name(label, db_name)
    if build_db in PROTECTED_TARGETS or build_db == source_db or build_db == target_db:
        raise ValueError("build_db must be isolated from source, target, and protected operating schemas")
    if target_db in PROTECTED_TARGETS and not allow_operating_target:
        raise RuntimeError("refusing operating target publish without --allow-operating-target")


def run_s4_general(
    *,
    build_db: str,
    source_db: str,
    catalog_root: Path | None,
    ubist_dir: Path | None,
    input_mode: str,
    sources: tuple[str, ...] | None = None,
) -> None:
    params = {
        "target_db": build_db,
        "source_db": source_db,
        "catalog_root": str(catalog_root) if catalog_root else None,
        "ubist_dir": str(ubist_dir) if ubist_dir else None,
        "input_mode": input_mode,
        "sources": sources,
    }
    rc = s4_mart.run(params)
    if rc != 0:
        raise RuntimeError(f"s4_mart failed rc={rc}")


def run_strategic_ml_market_from_source(*, build_db: str, source_db: str, catalog_root: Path | None) -> None:
    root = catalog_root or first_existing(PROJECT_ROOT / "output" / "catalog", PROJECT_ROOT / "parquet")
    catalog_rows = _load_ml_market_catalog(root)
    conn = connect_admin()
    try:
        _create_strategic_ml_market_table(conn, build_db)
        brand_rows = _fetch_strategic_ml_brand_rows(conn, source_db)
        market_rows = build_strategic_ml_market_rows(brand_rows, catalog_rows)
        _insert_strategic_ml_market_rows(conn, build_db, market_rows)
    finally:
        conn.close()
    print(
        "[strategic_ml_market] "
        f"source_db={source_db} brand_rows={len(brand_rows)} market_rows={len(market_rows)} target_db={build_db}"
    )


def build_strategic_ml_market_rows(
    brand_rows: list[dict[str, Any]],
    catalog_market_rows: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source_row in brand_rows:
        row = dict(source_row)
        row.setdefault("channel_specialty_matrix", {})
        grouped[(str(row["ml_id"]), str(row["source"]), str(row["measure"]))].append(row)

    market_rows: list[dict[str, Any]] = []
    for (ml_id, source, measure), members in sorted(grouped.items()):
        catalog_row = dict(catalog_market_rows.get(ml_id) or {})
        payload = compute_market_mart_payload(
            members,
            source=source,
            measure=measure,
            view_type="strategic_ml",
            catalog_market_row=catalog_row or None,
        )
        market_rows.append(
            {
                "ml_id": ml_id,
                "ml_name": catalog_row.get("name"),
                "source": source,
                "measure": measure,
                "unit_label": members[0].get("unit_label") or "",
                **payload,
            }
        )
    if not market_rows:
        raise RuntimeError(f"No {STRATEGIC_MARKET_TABLE} rows were built from {STRATEGIC_BRAND_TABLE}")
    return market_rows


def run_bridge(*, build_db: str, source_db: str, catalog_root: Path | None) -> None:
    root = catalog_root or first_existing(PROJECT_ROOT / "output" / "catalog", PROJECT_ROOT / "parquet")
    stats = build_molecule_bridge(source_db=source_db, target_db=build_db, catalog_root=root)
    print(
        "[mart_brand_molecule] "
        f"source_db={stats.source_db} target_db={stats.target_db} rows={stats.inserted_rows} "
        f"candidates={stats.candidate_rows} brand_keys={stats.brand_keys} molecules={stats.molecule_norms} "
        f"combo_rows={stats.combo_rows}"
    )


def publish_tables(
    conn: pymysql.connections.Connection,
    *,
    build_db: str,
    target_db: str,
    run_id: str,
    include_strategic_ml_market: bool,
    tables: tuple[str, ...] | None = None,
) -> tuple[PublishAction, ...]:
    selected = list(tables or MART_TABLES)
    if tables is None and include_strategic_ml_market:
        selected.append(STRATEGIC_MARKET_TABLE)
    actions: list[PublishAction] = []
    try:
        for table in selected:
            actions.append(_publish_one(conn, build_db, target_db, table, run_id))
    except Exception:
        restore_published_tables(
            conn,
            target_db=target_db,
            actions=tuple(reversed(actions)),
            run_id=run_id,
        )
        raise
    return tuple(actions)


def publish_table_group_atomically(
    conn: pymysql.connections.Connection,
    *,
    build_db: str,
    target_db: str,
    run_id: str,
    tables: tuple[str, ...],
) -> tuple[PublishAction, ...]:
    """Prepare every replacement, then swap the complete table group in one DDL."""

    if not tables:
        raise ValueError("atomic publish requires at least one table")
    prepared: list[tuple[str, str, str, int]] = []
    scratch_tables: list[str] = []
    try:
        for table in tables:
            if not table_exists(conn, build_db, table):
                raise RuntimeError(f"build table missing: {build_db}.{table}")
            if not table_exists(conn, target_db, table):
                raise RuntimeError(
                    f"atomic publish requires existing serving table: {target_db}.{table}"
                )
            new_table = f"{table}__new_{run_id}"
            backup_table = f"{table}__old_{run_id}"
            if table_exists(conn, target_db, new_table) or table_exists(conn, target_db, backup_table):
                raise RuntimeError(f"publish scratch table already exists for run_id={run_id}: {table}")
            build_rows = _table_row_count(conn, build_db, table)
            scratch_tables.append(new_table)
            _copy_table(conn, build_db, target_db, table, new_table)
            copied_rows = _table_row_count_with_bounded_retry(
                conn, target_db, new_table, expected=build_rows
            )
            if copied_rows != build_rows:
                raise RuntimeError(
                    f"{target_db}.{new_table} row count mismatch after copy: "
                    f"{copied_rows} != {build_rows}"
                )
            prepared.append((table, new_table, backup_table, build_rows))
        moves: list[str] = []
        for table, new_table, backup_table, _rows in prepared:
            moves.extend(
                (
                    f"{quote_id(target_db)}.{quote_id(table)} TO "
                    f"{quote_id(target_db)}.{quote_id(backup_table)}",
                    f"{quote_id(target_db)}.{quote_id(new_table)} TO "
                    f"{quote_id(target_db)}.{quote_id(table)}",
                )
            )
        with conn.cursor() as cur:
            cur.execute("RENAME TABLE " + ", ".join(moves))
    except Exception:
        for new_table in scratch_tables:
            if table_exists(conn, target_db, new_table):
                with conn.cursor() as cur:
                    cur.execute(
                        f"DROP TABLE {quote_id(target_db)}.{quote_id(new_table)}"
                    )
        raise
    return tuple(
        PublishAction(table, "atomic_group_rename", table, backup_table, rows)
        for table, _new_table, backup_table, rows in prepared
    )


def restore_table_group_atomically(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    actions: tuple[PublishAction, ...],
    run_id: str,
) -> None:
    """Restore a table group in one RENAME TABLE statement."""

    restorable = tuple(action for action in actions if action.backup_table)
    if not restorable:
        return
    moves: list[str] = []
    for action in restorable:
        failed_table = f"{action.table}__failed_{run_id}"
        if table_exists(conn, target_db, failed_table):
            raise RuntimeError(f"rollback scratch table already exists: {target_db}.{failed_table}")
        if not table_exists(conn, target_db, action.backup_table):
            raise RuntimeError(f"rollback backup table missing: {target_db}.{action.backup_table}")
        moves.extend(
            (
                f"{quote_id(target_db)}.{quote_id(action.table)} TO "
                f"{quote_id(target_db)}.{quote_id(failed_table)}",
                f"{quote_id(target_db)}.{quote_id(action.backup_table)} TO "
                f"{quote_id(target_db)}.{quote_id(action.table)}",
            )
        )
    with conn.cursor() as cur:
        cur.execute("RENAME TABLE " + ", ".join(moves))


def restore_published_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    actions: tuple[PublishAction, ...],
    run_id: str,
) -> None:
    """Restore tables already swapped by a partially failed publish."""

    for action in actions:
        if not action.backup_table:
            continue
        failed_table = f"{action.table}__failed_{run_id}"
        if table_exists(conn, target_db, failed_table):
            raise RuntimeError(f"rollback scratch table already exists: {target_db}.{failed_table}")
        if not table_exists(conn, target_db, action.backup_table):
            raise RuntimeError(f"rollback backup table missing: {target_db}.{action.backup_table}")
        with conn.cursor() as cur:
            cur.execute(
                f"RENAME TABLE {quote_id(target_db)}.{quote_id(action.table)} TO "
                f"{quote_id(target_db)}.{quote_id(failed_table)}, "
                f"{quote_id(target_db)}.{quote_id(action.backup_table)} TO "
                f"{quote_id(target_db)}.{quote_id(action.table)}"
            )


def dump_tables(
    *,
    target_db: str,
    tables: tuple[str, ...],
    dump_path: Path,
) -> DumpResult:
    dump_bin = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not dump_bin:
        raise RuntimeError("mariadb-dump/mysqldump not found in PATH")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    db_env = _db_env()
    password = db_env.get("MARIADB_ROOT_PASSWORD") or db_env.get("MARIADB_PASSWORD")
    user = "root" if db_env.get("MARIADB_ROOT_PASSWORD") else db_env.get("MARIADB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    env["MYSQL_PWD"] = password
    command = [
        dump_bin,
        f"--host={db_env.get('MARIADB_HOST', '127.0.0.1')}",
        f"--port={db_env.get('MARIADB_PORT') or db_env.get('HOST_PORT', '3307')}",
        f"--user={user}",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--skip-add-locks",
        "--skip-extended-insert",
        "--skip-disable-keys",
        "--skip-no-autocommit",
        target_db,
        *tables,
    ]
    start = time.perf_counter()
    if dump_path.suffix == ".gz":
        with subprocess.Popen(command, stdout=subprocess.PIPE, env=env) as proc:
            if proc.stdout is None:
                raise RuntimeError("dump command did not expose stdout")
            with gzip.open(dump_path, "wb") as out:
                shutil.copyfileobj(proc.stdout, out, length=1024 * 1024)
            return_code = proc.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
    else:
        with dump_path.open("wb") as out:
            subprocess.run(command, check=True, stdout=out, env=env)
    elapsed = time.perf_counter() - start
    return DumpResult(path=dump_path, size_bytes=dump_path.stat().st_size, elapsed_seconds=elapsed)


def _publish_one(
    conn: pymysql.connections.Connection,
    build_db: str,
    target_db: str,
    table_name: str,
    run_id: str,
) -> PublishAction:
    if not table_exists(conn, build_db, table_name):
        raise RuntimeError(f"build table missing: {build_db}.{table_name}")
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_id(target_db)} DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
    build_rows = _table_row_count(conn, build_db, table_name)
    if not table_exists(conn, target_db, table_name):
        _copy_table(conn, build_db, target_db, table_name, table_name)
        return PublishAction(table_name, "create", table_name, None, build_rows)
    new_table = f"{table_name}__new_{run_id}"
    backup_table = f"{table_name}__old_{run_id}"
    if table_exists(conn, target_db, new_table) or table_exists(conn, target_db, backup_table):
        raise RuntimeError(f"publish scratch table already exists for run_id={run_id}: {table_name}")
    _copy_table(conn, build_db, target_db, table_name, new_table)
    copied_rows = _table_row_count_with_bounded_retry(conn, target_db, new_table, expected=build_rows)
    if copied_rows != build_rows:
        raise RuntimeError(f"{target_db}.{new_table} row count mismatch after copy: {copied_rows} != {build_rows}")
    with conn.cursor() as cur:
        cur.execute(
            f"RENAME TABLE {quote_id(target_db)}.{quote_id(table_name)} TO {quote_id(target_db)}.{quote_id(backup_table)}, "
            f"{quote_id(target_db)}.{quote_id(new_table)} TO {quote_id(target_db)}.{quote_id(table_name)}"
        )
    return PublishAction(table_name, "atomic_rename", table_name, backup_table, build_rows)


def _copy_table(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    source_table: str,
    target_table: str,
    batch_size: int = 200,
) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    effective_batch_size = min(batch_size, 200)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {quote_id(target_db)}.{quote_id(target_table)} "
            f"LIKE {quote_id(source_db)}.{quote_id(source_table)}"
        )
    columns = _ordered_columns(conn, source_db, source_table)
    column_sql = ",".join(quote_id(column) for column in columns)
    if "id" not in columns:
        _copy_table_without_id(
            conn,
            source_db,
            target_db,
            source_table,
            target_table,
            columns,
            effective_batch_size,
        )
        return

    bounds = _id_bounds(conn, source_db, source_table)
    if bounds is None:
        return
    min_id, max_id = bounds
    with conn.cursor() as cur:
        for lower in range(min_id, max_id + 1, effective_batch_size):
            upper = lower + effective_batch_size - 1
            cur.execute(
                f"INSERT INTO {quote_id(target_db)}.{quote_id(target_table)} ({column_sql}) "
                f"SELECT {column_sql} FROM {quote_id(source_db)}.{quote_id(source_table)} "
                "WHERE id BETWEEN %s AND %s ORDER BY id",
                (lower, upper),
            )


def _copy_table_without_id(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    source_table: str,
    target_table: str,
    columns: list[str],
    batch_size: int,
) -> None:
    source_rows = _table_row_count(conn, source_db, source_table)
    if source_rows == 0:
        return
    order_columns = _primary_key_columns(conn, source_db, source_table) or columns
    if order_columns != columns:
        _copy_table_by_keyset(
            conn,
            source_db,
            target_db,
            source_table,
            target_table,
            columns,
            order_columns,
            batch_size,
        )
        return
    column_sql = ",".join(quote_id(column) for column in columns)
    order_sql = ",".join(quote_id(column) for column in order_columns)
    with conn.cursor() as cur:
        for offset in range(0, source_rows, batch_size):
            cur.execute(
                f"INSERT INTO {quote_id(target_db)}.{quote_id(target_table)} ({column_sql}) "
                f"SELECT {column_sql} FROM {quote_id(source_db)}.{quote_id(source_table)} "
                f"ORDER BY {order_sql} LIMIT {batch_size} OFFSET {offset}"
            )


def _copy_table_by_keyset(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    source_table: str,
    target_table: str,
    columns: list[str],
    key_columns: list[str],
    batch_size: int,
) -> None:
    column_sql = ",".join(quote_id(column) for column in columns)
    key_sql = ",".join(quote_id(column) for column in key_columns)
    order_sql = ",".join(quote_id(column) for column in key_columns)
    value_placeholders = ",".join(["%s"] * len(columns))
    last_key: tuple[Any, ...] | None = None
    while True:
        where_sql, where_params = _keyset_after_clause(key_columns, last_key)
        select_sql = (
            f"SELECT {column_sql} FROM {quote_id(source_db)}.{quote_id(source_table)} "
            f"{where_sql} ORDER BY {order_sql} LIMIT {batch_size}"
        )
        with conn.cursor() as cur:
            cur.execute(select_sql, where_params)
            rows = cur.fetchall()
        if not rows:
            return
        values = [tuple(row[column] for column in columns) for row in rows]
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {quote_id(target_db)}.{quote_id(target_table)} ({column_sql}) "
                f"VALUES ({value_placeholders})",
                values,
            )
        last_key = tuple(rows[-1][column] for column in key_columns)


def _keyset_after_clause(key_columns: list[str], last_key: tuple[Any, ...] | None) -> tuple[str, tuple[Any, ...]]:
    if last_key is None:
        return "", ()
    comparisons: list[str] = []
    params: list[Any] = []
    for index, column in enumerate(key_columns):
        parts = [f"{quote_id(previous)} = %s" for previous in key_columns[:index]]
        parts.append(f"{quote_id(column)} > %s")
        comparisons.append("(" + " AND ".join(parts) + ")")
        params.extend(last_key[:index])
        params.append(last_key[index])
    return "WHERE " + " OR ".join(comparisons), tuple(params)


def _ordered_columns(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {quote_id(db_name)}.{quote_id(table_name)}")
        return [str(row["Field"]) for row in cur.fetchall()]


def _primary_key_columns(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {quote_id(db_name)}.{quote_id(table_name)}")
        return [str(row["Field"]) for row in cur.fetchall() if str(row["Key"]) == "PRI"]


def _table_row_count(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS row_count FROM {quote_id(db_name)}.{quote_id(table_name)}")
        row = cur.fetchone()
    return int(row["row_count"])


def _table_row_count_with_bounded_retry(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
    *,
    expected: int,
) -> int:
    row_count = _table_row_count(conn, db_name, table_name)
    for _ in range(COUNT_READBACK_RETRY_LIMIT):
        if row_count == expected:
            return row_count
        time.sleep(COUNT_READBACK_RETRY_DELAY_SECONDS)
        row_count = _table_row_count(conn, db_name, table_name)
    return row_count


def _id_bounds(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> tuple[int, int] | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM {quote_id(db_name)}.{quote_id(table_name)}")
        row = cur.fetchone()
    if not row or row["min_id"] is None or row["max_id"] is None:
        return None
    return int(row["min_id"]), int(row["max_id"])


def _load_ml_market_catalog(catalog_root: Path) -> dict[str, dict[str, Any]]:
    path = catalog_root / "ml_market" / "ml_market.parquet"
    if not path.exists():
        raise FileNotFoundError(f"ml_market catalog not found: {path}")
    frame = pd.read_parquet(path)
    return {str(row["ml_id"]): row.to_dict() for _, row in frame.iterrows()}


def _create_strategic_ml_market_table(conn: pymysql.connections.Connection, build_db: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_id(build_db)} DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
        cur.execute(f"DROP TABLE IF EXISTS {quote_id(build_db)}.{quote_id(STRATEGIC_MARKET_TABLE)}")
        cur.execute(f"USE {quote_id(build_db)}")
        cur.execute(s5_mart.STRATEGIC_ML_MARKET_DDL)


def _fetch_strategic_ml_brand_rows(conn: pymysql.connections.Connection, source_db: str) -> list[dict[str, Any]]:
    available = _columns(conn, source_db, STRATEGIC_BRAND_TABLE)
    missing = [column for column in STRATEGIC_BRAND_REQUIRED_COLUMNS if column not in available]
    if missing:
        raise RuntimeError(f"{source_db}.{STRATEGIC_BRAND_TABLE} is missing required columns: {missing}")
    selected = [*STRATEGIC_BRAND_REQUIRED_COLUMNS, *(column for column in STRATEGIC_BRAND_OPTIONAL_JSON_COLUMNS if column in available)]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            + ",".join(quote_id(column) for column in selected)
            + f" FROM {quote_id(source_db)}.{quote_id(STRATEGIC_BRAND_TABLE)}"
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        for column in STRATEGIC_BRAND_JSON_COLUMNS:
            row[column] = _decode_json(row.get(column))
        row.setdefault("dimension_specialty_data", {})
        row.setdefault("channel_specialty_matrix", {})
        row.setdefault("ubist_channel_by_display", {})
        row.setdefault("ubist_channel_by_code", {})
    if not rows:
        raise RuntimeError(f"{source_db}.{STRATEGIC_BRAND_TABLE} returned no rows")
    return rows


def _insert_strategic_ml_market_rows(
    conn: pymysql.connections.Connection,
    build_db: str,
    rows: list[dict[str, Any]],
    batch_size: int = 200,
) -> None:
    json_columns = set(ML_MARKET_COLUMNS) - {"ml_id", "ml_name", "source", "measure", "unit_label"}
    placeholders = ",".join(["%s"] * len(ML_MARKET_COLUMNS))
    sql = (
        f"INSERT INTO {quote_id(build_db)}.{quote_id(STRATEGIC_MARKET_TABLE)} "
        f"({','.join(quote_id(column) for column in ML_MARKET_COLUMNS)}) VALUES ({placeholders})"
    )
    payloads = [
        tuple(dumps(row.get(column)) if column in json_columns else row.get(column) for column in ML_MARKET_COLUMNS)
        for row in rows
    ]
    with conn.cursor() as cur:
        for start in range(0, len(payloads), batch_size):
            cur.executemany(sql, payloads[start : start + batch_size])


def _columns(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> set[str]:
    if not table_exists(conn, db_name, table_name):
        raise RuntimeError(f"Missing source table: {db_name}.{table_name}")
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {quote_id(db_name)}.{quote_id(table_name)}")
        return {str(row["Field"]) for row in cur.fetchall()}


def _decode_json(value: Any) -> Any:
    if value in (None, "", b""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        return json.loads(text)
    return value


def _db_env() -> dict[str, str]:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    for key, value in os.environ.items():
        if key.startswith("MARIADB_") or key == "HOST_PORT":
            env[key] = value
    return env
