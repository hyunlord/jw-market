from __future__ import annotations

import os
from pathlib import Path

import pymysql

from .models import MessageRecord


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries from a local env file."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        values[key] = raw_value.strip().strip('"').strip("'")
    return values


def connect_mariadb(env_path: Path) -> pymysql.connections.Connection:
    """Open a read-oriented MariaDB connection using repo-local env defaults."""
    file_env = read_env_file(env_path)
    password = os.environ.get("MARIADB_ROOT_PASSWORD") or file_env.get("MARIADB_ROOT_PASSWORD", "")
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3308")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=password,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def validate_stage_schema(stage_schema: str) -> str:
    """Refuse schemas outside the isolated brand activity stage schema."""
    if stage_schema != "jw_brand_activity_stage":
        raise ValueError(f"refusing schema outside jw_brand_activity_stage: {stage_schema!r}")
    return stage_schema


def fetch_messages(connection: pymysql.connections.Connection, stage_schema: str) -> list[MessageRecord]:
    """Fetch Keyword plus auxiliary Meeting text using SELECT-only statements."""
    schema = validate_stage_schema(stage_schema)
    rows: list[MessageRecord] = []
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT stage_row_sha256, period_ym, product_name, therapeutic_class, keyword_text
            FROM {schema}.km_keyword_event_stage
            WHERE keyword_text <> ''
            """
        )
        for record in cursor.fetchall():
            rows.append(
                MessageRecord(
                    "keyword",
                    str(record["therapeutic_class"]),
                    f"kw:{record['stage_row_sha256']}",
                    str(record["period_ym"]),
                    str(record["product_name"]),
                    str(record["keyword_text"]),
                    1,
                )
            )
        cursor.execute(
            f"""
            SELECT stage_row_sha256, period_ym, product_name, therapeutic_class,
                   meeting_topic, verbatim_message
            FROM {schema}.km_meeting_event_stage
            """
        )
        for record in cursor.fetchall():
            for field, source in (("meeting_topic", "meeting_topic"), ("verbatim_message", "meeting_verbatim")):
                text = str(record[field] or "").strip()
                if not text:
                    continue
                rows.append(
                    MessageRecord(
                        source,
                        str(record["therapeutic_class"]),
                        f"{source}:{record['stage_row_sha256']}",
                        str(record["period_ym"]),
                        str(record["product_name"]),
                        text,
                        1,
                    )
                )
    return rows


def fetch_table_snapshot(connection: pymysql.connections.Connection, stage_schema: str) -> dict[str, int]:
    """Return row counts for stage tables used to prove the PoC made no writes."""
    schema = validate_stage_schema(stage_schema)
    tables = ["km_keyword_event_stage", "km_meeting_event_stage"]
    snapshot: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {schema}.{table}")
            snapshot[table] = int(cursor.fetchone()["row_count"])
    return snapshot
