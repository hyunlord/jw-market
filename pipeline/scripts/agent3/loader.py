from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .db import DbConfig, connect
from .json_util import canonical_json


DDL_PATHS = (
    Path(__file__).resolve().parent / "sql" / "002_recreate_agent3_brand_strength_brand_key.sql",
    Path(__file__).resolve().parent / "sql" / "003_add_serving_brand_name.sql",
)


@dataclass(frozen=True, slots=True)
class Agent3Record:
    brand_key: str
    brand_name: str
    serving_brand_name: str | None
    profile_json: dict[str, Any]
    strength_candidates_json: list[dict[str, Any]]
    strength_summary_json: dict[str, Any]
    workflow_id: int
    workflow_rev: int
    input_hash: str
    generated_at: datetime


def compute_input_hash(profile: dict[str, Any], candidates: list[dict[str, Any]], workflow_rev: int) -> str:
    payload = {"profile": profile, "candidates": candidates, "workflow_rev": workflow_rev}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Agent3Loader:
    def __init__(self, config: DbConfig | None = None) -> None:
        self.config = config or DbConfig.from_env()

    def ensure_table(self) -> None:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                for ddl_path in DDL_PATHS:
                    for statement in _ddl_statements(ddl_path):
                        cursor.execute(statement)
            conn.commit()

    def upsert(self, record: Agent3Record) -> int:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                _clear_serving_conflict(cursor, record)
                affected = cursor.execute(
                    """
                    INSERT INTO agent3_brand_strength
                      (brand_key, brand_name, serving_brand_name, profile_json, strength_candidates_json, strength_summary_json,
                       workflow_id, workflow_rev, input_hash, generated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      brand_name=VALUES(brand_name),
                      serving_brand_name=VALUES(serving_brand_name),
                      profile_json=VALUES(profile_json),
                      strength_candidates_json=VALUES(strength_candidates_json),
                      strength_summary_json=VALUES(strength_summary_json),
                      workflow_id=VALUES(workflow_id),
                      workflow_rev=VALUES(workflow_rev),
                      input_hash=VALUES(input_hash),
                      generated_at=VALUES(generated_at)
                    """,
                    (
                        record.brand_key,
                        record.brand_name,
                        record.serving_brand_name,
                        json.dumps(record.profile_json, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.strength_candidates_json, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.strength_summary_json, ensure_ascii=False, sort_keys=True),
                        record.workflow_id,
                        record.workflow_rev,
                        record.input_hash,
                        record.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            conn.commit()
        return int(affected)

    def load_existing_hashes(self, brand_keys: list[str]) -> dict[str, tuple[str, int]]:
        if not brand_keys:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_keys))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_key, input_hash, workflow_rev
                    FROM agent3_brand_strength
                    WHERE brand_key IN ({placeholders})
                    """,
                    tuple(brand_keys),
                )
                return {
                    str(row["brand_key"]): (str(row["input_hash"]), int(row["workflow_rev"]))
                    for row in cursor.fetchall()
                }

    def upsert_many(self, records: list[Agent3Record], *, batch_size: int = 200) -> int:
        total = 0
        if not records:
            return total
        sql = """
            INSERT INTO agent3_brand_strength
              (brand_key, brand_name, serving_brand_name, profile_json, strength_candidates_json, strength_summary_json,
               workflow_id, workflow_rev, input_hash, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              brand_name=VALUES(brand_name),
              serving_brand_name=VALUES(serving_brand_name),
              profile_json=VALUES(profile_json),
              strength_candidates_json=VALUES(strength_candidates_json),
              strength_summary_json=VALUES(strength_summary_json),
              workflow_id=VALUES(workflow_id),
              workflow_rev=VALUES(workflow_rev),
              input_hash=VALUES(input_hash),
              generated_at=VALUES(generated_at)
        """
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                for offset in range(0, len(records), batch_size):
                    for record in records[offset : offset + batch_size]:
                        _clear_serving_conflict(cursor, record)
                    params = [_record_params(record) for record in records[offset : offset + batch_size]]
                    total += int(cursor.executemany(sql, params))
            conn.commit()
        return total


def make_record(
    *,
    brand_key: str,
    brand_name: str,
    serving_brand_name: str | None,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    workflow_id: int,
    workflow_rev: int,
) -> Agent3Record:
    return Agent3Record(
        brand_key=brand_key,
        brand_name=brand_name,
        serving_brand_name=serving_brand_name,
        profile_json=profile,
        strength_candidates_json=candidates,
        strength_summary_json=summary,
        workflow_id=workflow_id,
        workflow_rev=workflow_rev,
        input_hash=compute_input_hash(profile, candidates, workflow_rev),
        generated_at=datetime.now(timezone.utc),
    )


def _record_params(record: Agent3Record) -> tuple[str, str, str | None, str, str, str, int, int, str, str]:
    return (
        record.brand_key,
        record.brand_name,
        record.serving_brand_name,
        json.dumps(record.profile_json, ensure_ascii=False, sort_keys=True),
        json.dumps(record.strength_candidates_json, ensure_ascii=False, sort_keys=True),
        json.dumps(record.strength_summary_json, ensure_ascii=False, sort_keys=True),
        record.workflow_id,
        record.workflow_rev,
        record.input_hash,
        record.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _clear_serving_conflict(cursor: Any, record: Agent3Record) -> None:
    if record.serving_brand_name is None:
        return
    cursor.execute(
        """
        UPDATE agent3_brand_strength
        SET serving_brand_name=NULL
        WHERE serving_brand_name=%s AND brand_key<>%s
        """,
        (record.serving_brand_name, record.brand_key),
    )


def _ddl_statements(path: Path) -> list[str]:
    return [statement.strip() for statement in path.read_text(encoding="utf-8").split(";") if statement.strip()]
