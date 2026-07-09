from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from .db import DbConfig, connect
from .json_util import canonical_json


Agent3Source = Literal["iqvia", "ubist"]
SOURCE_DDL_PATH = Path(__file__).resolve().parent / "sql" / "004_create_agent3_brand_strength_source.sql"


@dataclass(frozen=True, slots=True)
class ExistingAgent3SourceState:
    input_hash: str
    workflow_rev: int


@dataclass(frozen=True, slots=True)
class Agent3SourceRecord:
    brand_key: str
    source: Agent3Source
    brand_name: str
    serving_brand_name: str | None
    profile_json: dict[str, Any]
    strength_candidates_json: list[dict[str, Any]]
    strength_summary_json: dict[str, Any]
    workflow_id: int
    workflow_rev: int
    input_hash: str
    generated_at: datetime


def compute_source_input_hash(
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    workflow_rev: int,
    source: Agent3Source,
) -> str:
    payload = {"profile": profile, "candidates": candidates, "workflow_rev": workflow_rev, "source": source}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_source_record(
    *,
    brand_key: str,
    source: Agent3Source,
    brand_name: str,
    serving_brand_name: str | None,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    workflow_id: int,
    workflow_rev: int,
) -> Agent3SourceRecord:
    return Agent3SourceRecord(
        brand_key=brand_key,
        source=source,
        brand_name=brand_name,
        serving_brand_name=serving_brand_name,
        profile_json=profile,
        strength_candidates_json=candidates,
        strength_summary_json=summary,
        workflow_id=workflow_id,
        workflow_rev=workflow_rev,
        input_hash=compute_source_input_hash(profile, candidates, workflow_rev, source),
        generated_at=datetime.now(timezone.utc),
    )


class Agent3SourceLoader:
    def __init__(self, config: DbConfig | None = None) -> None:
        self.config = config or DbConfig.from_env()

    def ensure_table(self) -> None:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(SOURCE_DDL_PATH.read_text(encoding="utf-8"))
            conn.commit()

    def load_existing_hashes(self, brand_keys: list[str]) -> dict[tuple[str, Agent3Source], ExistingAgent3SourceState]:
        if not brand_keys:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_keys))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_key, source, input_hash, workflow_rev
                    FROM agent3_brand_strength_source
                    WHERE brand_key IN ({placeholders})
                    """,
                    tuple(brand_keys),
                )
                rows = cursor.fetchall()
        return {
            (str(row["brand_key"]), _parse_source(str(row["source"]))): ExistingAgent3SourceState(
                input_hash=str(row["input_hash"]),
                workflow_rev=int(row["workflow_rev"]),
            )
            for row in rows
        }

    def upsert_many(self, records: list[Agent3SourceRecord], *, batch_size: int = 200) -> int:
        if not records:
            return 0
        sql = """
            INSERT INTO agent3_brand_strength_source
              (brand_key, source, brand_name, serving_brand_name, profile_json, strength_candidates_json,
               strength_summary_json, workflow_id, workflow_rev, input_hash, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        total = 0
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                for offset in range(0, len(records), batch_size):
                    params = [_record_params(record) for record in records[offset : offset + batch_size]]
                    total += int(cursor.executemany(sql, params))
            conn.commit()
        return total

    def load_factor_sections(self, brand_keys: list[str]) -> dict[tuple[str, Agent3Source], dict[str, Any]]:
        if not brand_keys:
            return {}
        placeholders = ", ".join(["%s"] * len(brand_keys))
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_key, source, profile_json, strength_summary_json
                    FROM agent3_brand_strength_source
                    WHERE brand_key IN ({placeholders})
                    """,
                    tuple(brand_keys),
                )
                rows = cursor.fetchall()
        return {
            (str(row["brand_key"]), _parse_source(str(row["source"]))): {
                "profile": json.loads(str(row["profile_json"])),
                "strength_items": _strength_items(row["strength_summary_json"]),
            }
            for row in rows
        }


def _record_params(record: Agent3SourceRecord) -> tuple[str, str, str, str | None, str, str, str, int, int, str, str]:
    return (
        record.brand_key,
        record.source,
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


def _parse_source(value: str) -> Agent3Source:
    normalized = value.lower()
    if normalized == "iqvia":
        return "iqvia"
    if normalized == "ubist":
        return "ubist"
    raise ValueError(f"unsupported Agent3 source: {value}")  # noqa: GENERIC_ERR_OK


def _strength_items(value: Any) -> list[dict[str, Any]]:
    payload = value if isinstance(value, dict) else json.loads(str(value))
    items = payload.get("strength_items")
    return items if isinstance(items, list) else []
