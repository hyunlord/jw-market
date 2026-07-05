from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .db import DbConfig, connect
from .json_util import canonical_json


DDL_PATH = Path(__file__).resolve().parent / "sql" / "001_create_agent3_brand_strength.sql"


@dataclass(frozen=True, slots=True)
class Agent3Record:
    brand_name: str
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
        ddl = DDL_PATH.read_text(encoding="utf-8")
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(ddl)
            conn.commit()

    def upsert(self, record: Agent3Record) -> int:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                affected = cursor.execute(
                    """
                    INSERT INTO agent3_brand_strength
                      (brand_name, profile_json, strength_candidates_json, strength_summary_json,
                       workflow_id, workflow_rev, input_hash, generated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      profile_json=VALUES(profile_json),
                      strength_candidates_json=VALUES(strength_candidates_json),
                      strength_summary_json=VALUES(strength_summary_json),
                      workflow_id=VALUES(workflow_id),
                      workflow_rev=VALUES(workflow_rev),
                      input_hash=VALUES(input_hash),
                      generated_at=VALUES(generated_at)
                    """,
                    (
                        record.brand_name,
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

    def load_existing_hashes(self, brand_names: list[str]) -> dict[str, tuple[str, int]]:
        if not brand_names:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_names))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_name, input_hash, workflow_rev
                    FROM agent3_brand_strength
                    WHERE brand_name IN ({placeholders})
                    """,
                    tuple(brand_names),
                )
                return {
                    str(row["brand_name"]): (str(row["input_hash"]), int(row["workflow_rev"]))
                    for row in cursor.fetchall()
                }

    def upsert_many(self, records: list[Agent3Record], *, batch_size: int = 200) -> int:
        total = 0
        if not records:
            return total
        sql = """
            INSERT INTO agent3_brand_strength
              (brand_name, profile_json, strength_candidates_json, strength_summary_json,
               workflow_id, workflow_rev, input_hash, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
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
                    params = [_record_params(record) for record in records[offset : offset + batch_size]]
                    total += int(cursor.executemany(sql, params))
            conn.commit()
        return total


def make_record(
    *,
    brand_name: str,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    workflow_id: int,
    workflow_rev: int,
) -> Agent3Record:
    return Agent3Record(
        brand_name=brand_name,
        profile_json=profile,
        strength_candidates_json=candidates,
        strength_summary_json=summary,
        workflow_id=workflow_id,
        workflow_rev=workflow_rev,
        input_hash=compute_input_hash(profile, candidates, workflow_rev),
        generated_at=datetime.now(timezone.utc),
    )


def _record_params(record: Agent3Record) -> tuple[str, str, str, str, int, int, str, str]:
    return (
        record.brand_name,
        json.dumps(record.profile_json, ensure_ascii=False, sort_keys=True),
        json.dumps(record.strength_candidates_json, ensure_ascii=False, sort_keys=True),
        json.dumps(record.strength_summary_json, ensure_ascii=False, sort_keys=True),
        record.workflow_id,
        record.workflow_rev,
        record.input_hash,
        record.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )
