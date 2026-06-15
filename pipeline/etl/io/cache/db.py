from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pymysql

ROOT = Path(__file__).resolve().parents[4]
ENV_PATH = ROOT / "pipeline" / "docker" / ".env"
SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")


class UnsafeDatabaseNameError(RuntimeError):
    pass


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def database_name(value: str | None) -> str:
    if not value or not SAFE_DB_RE.fullmatch(value):
        raise UnsafeDatabaseNameError(f"unsafe database name: {value!r}")
    return value


@contextmanager
def connect(database: str | None = None) -> Iterator[pymysql.connections.Connection]:
    load_env()
    password = os.environ.get("MYSQL_PWD") or os.environ.get("MARIADB_ROOT_PASSWORD")
    conn = pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3308")),
        user=os.environ.get("CACHE_MARIADB_USER", "root"),
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        yield conn
    finally:
        conn.close()


def ensure_isolated_target(target_db: str, protected_dbs: tuple[str, ...]) -> str:
    name = database_name(target_db)
    protected = {database_name(item) for item in protected_dbs if item}
    if name in protected or not name.startswith("jw_mart_s6_"):
        raise UnsafeDatabaseNameError(f"refusing unsafe cache target database: {name}")
    return name


def recreate_database(target_db: str, *, protected_dbs: tuple[str, ...]) -> None:
    name = ensure_isolated_target(target_db, protected_dbs)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci")


def table_exists(source_db: str, table: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s
            """,
            (source_db, table),
        )
        return int(cur.fetchone()["cnt"]) > 0


def _source_row_count(cur: Any, source: str, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) AS cnt FROM `{source}`.`{table}`")
    return int(cur.fetchone()["cnt"])


def _primary_key_columns(cur: Any, source: str, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.key_column_usage
        WHERE table_schema=%s
          AND table_name=%s
          AND constraint_name='PRIMARY'
        ORDER BY ordinal_position
        """,
        (source, table),
    )
    return [str(row["column_name"]) for row in cur.fetchall()]


def _copy_by_id(cur: Any, source: str, target: str, table: str, batch_size: int) -> None:
    last_id = 0
    while True:
        affected = cur.execute(
            f"INSERT INTO `{target}`.`{table}` "
            f"SELECT * FROM `{source}`.`{table}` "
            f"WHERE `id` > {last_id} ORDER BY `id` LIMIT {batch_size}"
        )
        if affected == 0:
            return
        cur.execute(f"SELECT MAX(`id`) AS max_id FROM `{target}`.`{table}`")
        last_id = int(cur.fetchone()["max_id"])


def copy_table(source_db: str, target_db: str, table: str, *, batch_size: int = 500) -> int:
    source = database_name(source_db)
    target = database_name(target_db)
    if not table_exists(source, table):
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{target}`.`{table}`")
        cur.execute(f"CREATE TABLE `{target}`.`{table}` LIKE `{source}`.`{table}`")
        total = _source_row_count(cur, source, table)
        if _primary_key_columns(cur, source, table) == ["id"]:
            _copy_by_id(cur, source, target, table, batch_size)
        else:
            for offset in range(0, total, batch_size):
                cur.execute(
                    f"INSERT INTO `{target}`.`{table}` "
                    f"SELECT * FROM `{source}`.`{table}` LIMIT {batch_size} OFFSET {offset}"
                )
        cur.execute(f"SELECT COUNT(*) AS cnt FROM `{target}`.`{table}`")
        return int(cur.fetchone()["cnt"])


def copy_inputs(
    *,
    general_db: str,
    strategic_db: str,
    target_db: str,
    event_db: str = "jw_mart",
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("mart_general_brand_metric", "mart_general_market_metric"):
        counts[table] = copy_table(general_db, target_db, table)
    for table in (
        "mart_strategic_ml_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_cd_market_metric",
    ):
        counts[table] = copy_table(strategic_db, target_db, table)
    for table in ("news_raw", "event_brand_scores", "events"):
        counts[table] = copy_table(event_db, target_db, table)
    return counts


def table_counts(target_db: str, tables: tuple[str, ...]) -> dict[str, int]:
    database_name(target_db)
    out: dict[str, int] = {}
    with connect() as conn, conn.cursor() as cur:
        for table in tables:
            if not table_exists(target_db, table):
                out[table] = 0
                continue
            cur.execute(f"SELECT COUNT(*) AS cnt FROM `{target_db}`.`{table}`")
            out[table] = int(cur.fetchone()["cnt"])
    return out


def drop_old_cache_cause_tables(target_db: str) -> list[str]:
    target = database_name(target_db)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema=%s
              AND table_name LIKE 'cache_cause_old_fullregen_%%'
            """,
            (target,),
        )
        names = [str(row["table_name"]) for row in cur.fetchall()]
        for name in names:
            cur.execute(f"DROP TABLE `{target}`.`{name}`")
        return names
