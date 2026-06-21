"""Read-only MariaDB boundary for the topic redesign PoC."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import os
from pathlib import Path

import pymysql

from .dictionary import MARKET_TEMPLATES
from .models import JsonValue, MessageRow
from .text import normalize_text


REPO_ROOT = Path(__file__).resolve().parents[5]
ENV_PATH = REPO_ROOT / "pipeline/docker/.env"
SCHEMA = "jw_brand_activity_stage"
EXPECTED_KEYWORD_COLUMNS = {"id", "period_ym", "product_name", "therapeutic_class", "keyword_text", "stage_row_sha256"}
EXPECTED_MEETING_COLUMNS = {"id", "period_ym", "product_name", "therapeutic_class", "meeting_topic", "verbatim_message", "stage_row_sha256"}


class StageSchemaError(RuntimeError):
    """Raised when the local stage schema does not match the PoC contract."""


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Read local KEY=VALUE credentials without printing their values."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        values[key] = raw_value.strip().strip('"').strip("'")
    return values


def connect_mariadb(env: dict[str, str]) -> pymysql.connections.Connection:
    """Open a local MariaDB connection used only for read-only transactions."""
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", env.get("HOST_PORT", "3308"))),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_ROOT_PASSWORD", env["MARIADB_ROOT_PASSWORD"]),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_db_payload(connection: pymysql.connections.Connection) -> dict[str, JsonValue | list[MessageRow]]:
    """Fetch schema, snapshots, Keyword rows, and auxiliary Meeting rows in read-only mode."""
    columns = fetch_columns(connection)
    require_columns(columns)
    before = fetch_snapshot(connection)
    keyword_rows, auxiliary_rows = fetch_rows(connection)
    after = fetch_snapshot(connection)
    return {
        "columns": columns,
        "before_snapshot": before,
        "after_snapshot": after,
        "keyword_rows": keyword_rows,
        "auxiliary_rows": auxiliary_rows,
        "read_only_equal": before == after,
    }


def fetch_columns(connection: pymysql.connections.Connection) -> dict[str, list[str]]:
    """Return table columns for stage schema validation."""
    result: dict[str, list[str]] = {}
    with connection.cursor() as cursor:
        for table in ("km_keyword_event_stage", "km_meeting_event_stage"):
            cursor.execute(f"SHOW COLUMNS FROM {SCHEMA}.{table}")
            result[table] = [str(row["Field"]) for row in cursor.fetchall()]
    return result


def require_columns(columns: dict[str, list[str]]) -> None:
    """Fail fast if the local stage schema lacks required read columns."""
    keyword_missing = EXPECTED_KEYWORD_COLUMNS - set(columns["km_keyword_event_stage"])
    meeting_missing = EXPECTED_MEETING_COLUMNS - set(columns["km_meeting_event_stage"])
    if keyword_missing or meeting_missing:
        raise StageSchemaError(f"missing columns keyword={sorted(keyword_missing)} meeting={sorted(meeting_missing)}")


def fetch_snapshot(connection: pymysql.connections.Connection) -> dict[str, JsonValue]:
    """Measure row counts and stage-hash fingerprints before/after analysis."""
    snapshot: dict[str, JsonValue] = {}
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        for table in ("km_keyword_event_stage", "km_meeting_event_stage"):
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {SCHEMA}.{table}")
            row_count = int(cursor.fetchone()["row_count"])
            cursor.execute(f"SELECT stage_row_sha256 FROM {SCHEMA}.{table} ORDER BY id")
            digest = hashlib.sha256()
            for row in cursor.fetchall():
                digest.update(str(row["stage_row_sha256"]).encode("utf-8"))
                digest.update(b"\n")
            snapshot[table] = {"rows": row_count, "stage_hash_fingerprint": digest.hexdigest()}
        cursor.execute("COMMIT")
    return snapshot


def fetch_rows(connection: pymysql.connections.Connection) -> tuple[list[MessageRow], list[MessageRow]]:
    """Read Keyword denominator rows and Meeting auxiliary vocabulary rows."""
    keyword_rows: list[MessageRow] = []
    auxiliary_rows: list[MessageRow] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(f"SELECT id, period_ym, product_name, therapeutic_class, keyword_text, stage_row_sha256 FROM {SCHEMA}.km_keyword_event_stage WHERE keyword_text <> '' ORDER BY id")
        for record in cursor.fetchall():
            keyword_rows.append(_message_row("keyword", record, "keyword_text"))
        cursor.execute(f"SELECT id, period_ym, product_name, therapeutic_class, meeting_topic, verbatim_message, stage_row_sha256 FROM {SCHEMA}.km_meeting_event_stage ORDER BY id")
        for record in cursor.fetchall():
            for field, source in (("meeting_topic", "meeting_topic"), ("verbatim_message", "meeting_verbatim")):
                if normalize_text(str(record[field] or "")):
                    auxiliary_rows.append(_message_row(source, record, field))
        cursor.execute("COMMIT")
    return keyword_rows, auxiliary_rows


def validate_market_scope(markets: tuple[str, ...]) -> None:
    """Enforce the requested 17-market latest Keyword scope."""
    expected = set(MARKET_TEMPLATES)
    discovered = set(markets)
    if len(markets) != 17 or discovered != expected:
        raise StageSchemaError(f"expected 17 known markets, got {len(markets)} discovered={sorted(discovered)} expected={sorted(expected)}")


def count_by_market(rows: list[MessageRow]) -> dict[str, int]:
    """Count Keyword rows by ATC4 market."""
    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.market] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def group_by_market(rows: list[MessageRow]) -> dict[str, list[MessageRow]]:
    """Group rows by ATC4 market."""
    grouped: defaultdict[str, list[MessageRow]] = defaultdict(list)
    for row in rows:
        grouped[row.market].append(row)
    return dict(grouped)


def _message_row(source: str, record: dict[str, JsonValue], text_field: str) -> MessageRow:
    """Convert a stage DB record into the internal message row contract."""
    text = normalize_text(str(record[text_field] or ""))
    return MessageRow(source, f"{source}:{record['id']}", str(record["therapeutic_class"]), str(record["period_ym"]), str(record["product_name"] or ""), text, str(record["stage_row_sha256"]))

