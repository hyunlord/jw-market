from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
import hashlib
import os

import pymysql

from .models import JsonValue, KeywordRow
from .privacy import estimate_tokens


REPO_ROOT = Path(__file__).resolve().parents[5]
ENV_PATH = REPO_ROOT / "pipeline/docker/.env"
SCHEMA = "jw_brand_activity_stage"
KEYWORD_TABLE = "km_keyword_event_stage"
CSD_TABLE = "csd_channel_dynamics_stage"
DICTIONARY_PATH = REPO_ROOT / "docs/research/brand_activity/topic_redesign/REDESIGN_03_DICTIONARY_DRAFT.json"


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Read the local MariaDB env file without writing any environment state."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    return values


def connect_mariadb(env: dict[str, str]) -> pymysql.connections.Connection:
    """Open a local MariaDB connection for read-only analysis queries."""
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", env.get("HOST_PORT", "3308"))),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_ROOT_PASSWORD", env["MARIADB_ROOT_PASSWORD"]),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_keyword_rows(connection: pymysql.connections.Connection, atc4_values: Sequence[str]) -> list[KeywordRow]:
    """Fetch keyword rows for selected ATC4 values inside a read-only transaction."""
    placeholders = ",".join(["%s"] * len(atc4_values))
    sql = (
        "SELECT id, period_ym, visit_location, specialty, product_name, therapeutic_class, "
        "keyword_text, interest, prescription_frequency, prescription_evolution, "
        "abstract_lit, patient_lit, promotional_lit, stage_row_sha256 "
        f"FROM {SCHEMA}.{KEYWORD_TABLE} "
        f"WHERE keyword_text <> '' AND therapeutic_class IN ({placeholders}) "
        "ORDER BY therapeutic_class, product_name, period_ym, id"
    )
    rows: list[KeywordRow] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql, tuple(atc4_values))
        for record in cursor.fetchall():
            rows.append(_keyword_row(record))
        cursor.execute("COMMIT")
    return rows


def fetch_snapshot(connection: pymysql.connections.Connection) -> dict[str, JsonValue]:
    """Fingerprint stage tables before and after the analysis run."""
    snapshot: dict[str, JsonValue] = {}
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        for table, hash_column in ((KEYWORD_TABLE, "stage_row_sha256"), (CSD_TABLE, "master_product")):
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {SCHEMA}.{table}")
            row_count = int(cursor.fetchone()["row_count"])
            cursor.execute(f"SELECT {hash_column} AS value FROM {SCHEMA}.{table} ORDER BY 1")
            digest = hashlib.sha256()
            for row in cursor.fetchall():
                digest.update(str(row["value"]).encode("utf-8"))
                digest.update(b"\n")
            snapshot[table] = {"row_count": row_count, "fingerprint": digest.hexdigest()}
        cursor.execute("COMMIT")
    return snapshot


def keyword_stats(rows: list[KeywordRow]) -> dict[str, dict[str, JsonValue]]:
    """Summarize selected keyword rows by ATC4 for audit."""
    grouped: defaultdict[str, list[KeywordRow]] = defaultdict(list)
    for row in rows:
        grouped[row.atc4].append(row)
    return {
        atc4: {
            "row_count": len(items),
            "brand_count": len({item.brand for item in items}),
            "estimated_tokens": sum(estimate_tokens(item.keyword_text) for item in items),
            "brands": sorted({item.brand for item in items}),
        }
        for atc4, items in sorted(grouped.items())
    }


def deterministic_sample(rows: list[KeywordRow], *, limit: int, seed: str) -> list[KeywordRow]:
    """Select a deterministic sample without relying on DB order randomness."""
    ranked = sorted(rows, key=lambda row: hashlib.sha256(f"{seed}:{row.stage_row_sha256}".encode("utf-8")).hexdigest())
    return sorted(ranked[:limit], key=lambda row: row.row_id)


def sample_scope_rows(rows: list[KeywordRow], brands: Sequence[str], *, per_brand: int, seed: str) -> list[KeywordRow]:
    """Create a stratified prompt sample for a market or group scope."""
    sampled: list[KeywordRow] = []
    for brand in brands:
        sampled.extend(deterministic_sample([row for row in rows if row.brand == brand], limit=per_brand, seed=f"{seed}:{brand}"))
    return sorted(sampled, key=lambda row: (row.atc4, row.brand, row.row_id))


def sample_brand_rows(rows: list[KeywordRow], atc4: str, brand: str, *, limit: int) -> list[KeywordRow]:
    """Create a deterministic sample for one brand-share call."""
    matching = [row for row in rows if row.atc4 == atc4 and row.brand == brand]
    return deterministic_sample(matching, limit=limit, seed=f"brand:{atc4}:{brand}")


def _keyword_row(record: dict[str, JsonValue]) -> KeywordRow:
    """Convert a MariaDB record into the in-memory prompt row model."""
    return KeywordRow(
        row_id=int(record["id"]),
        period_ym=str(record["period_ym"] or ""),
        atc4=str(record["therapeutic_class"] or ""),
        brand=str(record["product_name"] or ""),
        keyword_text=" ".join(str(record["keyword_text"] or "").split()),
        interest=str(record["interest"] or ""),
        prescription_frequency=str(record["prescription_frequency"] or ""),
        prescription_evolution=str(record["prescription_evolution"] or ""),
        promotional_lit=str(record["promotional_lit"] or ""),
        abstract_lit=str(record["abstract_lit"] or ""),
        patient_lit=str(record["patient_lit"] or ""),
        specialty=str(record["specialty"] or ""),
        visit_location=str(record["visit_location"] or ""),
        stage_row_sha256=str(record["stage_row_sha256"] or ""),
    )
