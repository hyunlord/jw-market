from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .db import DbConfig, connect
from .json_util import canonical_json
from .market_repository import MarketViewKind
from .source_loader import Agent3Source


MARKET_DDL_PATH = Path(__file__).resolve().parent / "sql" / "005_create_agent3_brand_strength_market.sql"


def _strip_market_id_keys(value: Any) -> Any:
    """Recursively drop every ``market_id`` key from a JSON-like structure.

    market_id is forbidden in serving payloads (brand + view type suffice), so it must
    not appear inside the stored profile/candidate/summary blobs. Nested dicts and dicts
    inside lists are cleaned; all other keys, values, and order are preserved so that a
    canonical re-serialization differs from the original only by the removed key.
    """
    if isinstance(value, dict):
        return {key: _strip_market_id_keys(item) for key, item in value.items() if key != "market_id"}
    if isinstance(value, list):
        return [_strip_market_id_keys(item) for item in value]
    return value


def _count_market_id_keys(value: Any) -> int:
    """Return how many ``market_id`` keys exist at any depth (dicts and lists)."""
    if isinstance(value, dict):
        return sum((1 if key == "market_id" else 0) + _count_market_id_keys(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(_count_market_id_keys(item) for item in value)
    return 0


def _reject_market_id_contamination(records: list["MarketStrengthRecord"]) -> None:
    """Pre-write gate: refuse to persist any record whose payloads still carry market_id.

    make_market_record strips market_id, so a non-zero count here means a caller built a
    record bypassing the sanitizer. Failing closed keeps the forbidden key out of serving.
    """
    for record in records:
        residual = (
            _count_market_id_keys(record.profile_json)
            + _count_market_id_keys(record.strength_candidates_json)
            + _count_market_id_keys(record.strength_summary_json)
        )
        if residual:
            raise ValueError(
                "refusing market_id-contaminated write: "
                f"{record.brand_key}/{record.source}/{record.market_id} has {residual} "
                "market_id key(s) in stored payloads"
            )


@dataclass(frozen=True, slots=True)
class ExistingMarketState:
    view_kind: MarketViewKind
    input_hash: str
    workflow_rev: int
    profile_json: dict[str, Any]
    strength_candidates_json: list[dict[str, Any]]
    strength_summary_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MarketStrengthRecord:
    brand_key: str
    source: Agent3Source
    market_id: str
    view_kind: MarketViewKind
    brand_name: str
    serving_brand_name: str | None
    profile_json: dict[str, Any]
    strength_candidates_json: list[dict[str, Any]]
    strength_summary_json: dict[str, Any]
    workflow_id: int
    workflow_rev: int
    input_hash: str
    generation_status: str
    generated_at: datetime


def compute_market_input_hash(
    *,
    view_kind: MarketViewKind,
    market_id: str,
    brand_key: str,
    source: Agent3Source,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    workflow_rev: int,
) -> str:
    payload = {
        "view_kind": view_kind,
        "market_id": market_id,
        "brand_key": brand_key,
        "source": source,
        "profile": profile,
        "candidates": candidates,
        "workflow_rev": workflow_rev,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_market_record(
    *,
    brand_key: str,
    source: Agent3Source,
    market_id: str,
    view_kind: MarketViewKind,
    brand_name: str,
    serving_brand_name: str | None,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    workflow_id: int,
    workflow_rev: int,
    generation_status: str,
    hash_candidates: list[dict[str, Any]] | None = None,
) -> MarketStrengthRecord:
    input_candidates = candidates if hash_candidates is None else hash_candidates
    # Compute input_hash from the ORIGINAL (pre-strip) profile/candidates so existing
    # stored input_hash values are unchanged and the same-hash skip path keeps working
    # (no mass rewrite). Realigning the hash to the stripped payload is a later A-round.
    input_hash = compute_market_input_hash(
        view_kind=view_kind,
        market_id=market_id,
        brand_key=brand_key,
        source=source,
        profile=profile,
        candidates=input_candidates,
        workflow_rev=workflow_rev,
    )
    # Design B (boundary sanitize): strip market_id from the STORED payloads only, after
    # hashing. Generation never reads market_id back from these dicts (it uses
    # unit.market_id), and content-match compares these stripped payloads, so clean rows
    # stay clean across regen instead of being re-contaminated.
    return MarketStrengthRecord(
        brand_key=brand_key,
        source=source,
        market_id=market_id,
        view_kind=view_kind,
        brand_name=brand_name,
        serving_brand_name=serving_brand_name,
        profile_json=_strip_market_id_keys(profile),
        strength_candidates_json=_strip_market_id_keys(candidates),
        strength_summary_json=_strip_market_id_keys(summary),
        workflow_id=workflow_id,
        workflow_rev=workflow_rev,
        input_hash=input_hash,
        generation_status=generation_status,
        generated_at=datetime.now(timezone.utc),
    )


def canonical_market_content_matches(old: ExistingMarketState, new: MarketStrengthRecord) -> bool:
    if old.view_kind != new.view_kind:
        raise RuntimeError(
            f"strategic view_kind collision: {new.brand_key}/{new.source}/{new.market_id} "
            f"stored={old.view_kind} incoming={new.view_kind}"
        )
    return canonical_json(
        {
            "profile": old.profile_json,
            "candidates": old.strength_candidates_json,
            "summary": old.strength_summary_json,
        }
    ) == canonical_json(
        {
            "profile": new.profile_json,
            "candidates": new.strength_candidates_json,
            "summary": new.strength_summary_json,
        }
    )


class Agent3MarketLoader:
    def __init__(self, config: DbConfig | None = None) -> None:
        self.config = config or DbConfig.from_env()

    def ensure_table(self) -> None:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(MARKET_DDL_PATH.read_text(encoding="utf-8"))
            conn.commit()

    def load_existing(self) -> dict[tuple[str, Agent3Source, str], ExistingMarketState]:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT brand_key, source, market_id, view_kind, input_hash, workflow_rev,
                           profile_json, strength_candidates_json, strength_summary_json
                    FROM agent3_brand_strength_market
                    """
                )
                rows = cursor.fetchall()
        return {
            (str(row["brand_key"]), _source(str(row["source"])), str(row["market_id"])): ExistingMarketState(
                view_kind=_view_kind(str(row["view_kind"])),
                input_hash=str(row["input_hash"]),
                workflow_rev=int(row["workflow_rev"]),
                profile_json=_json_object(row["profile_json"]),
                strength_candidates_json=_json_list(row["strength_candidates_json"]),
                strength_summary_json=_json_object(row["strength_summary_json"]),
            )
            for row in rows
        }

    def upsert_many(self, records: list[MarketStrengthRecord]) -> int:
        if not records:
            return 0
        _reject_market_id_contamination(records)
        sql = """
            INSERT INTO agent3_brand_strength_market
              (brand_key, source, market_id, view_kind, brand_name, serving_brand_name,
               profile_json, strength_candidates_json, strength_summary_json,
               workflow_id, workflow_rev, input_hash, generation_status, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              brand_name=VALUES(brand_name), serving_brand_name=VALUES(serving_brand_name),
              profile_json=VALUES(profile_json),
              strength_candidates_json=VALUES(strength_candidates_json),
              strength_summary_json=VALUES(strength_summary_json),
              workflow_id=VALUES(workflow_id), workflow_rev=VALUES(workflow_rev),
              input_hash=VALUES(input_hash), generation_status=VALUES(generation_status),
              generated_at=VALUES(generated_at)
        """
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                affected = int(cursor.executemany(sql, [_params(record) for record in records]))
            conn.commit()
        return affected


def _params(record: MarketStrengthRecord) -> tuple[Any, ...]:
    return (
        record.brand_key,
        record.source,
        record.market_id,
        record.view_kind,
        record.brand_name,
        record.serving_brand_name,
        canonical_json(record.profile_json),
        canonical_json(record.strength_candidates_json),
        canonical_json(record.strength_summary_json),
        record.workflow_id,
        record.workflow_rev,
        record.input_hash,
        record.generation_status,
        record.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _source(value: str) -> Agent3Source:
    if value in {"iqvia", "ubist"}:
        return value
    raise ValueError(f"unsupported Agent3 source: {value}")


def _view_kind(value: str) -> MarketViewKind:
    if value in {"market_landscape", "competitive_dynamics"}:
        return value
    raise ValueError(f"unsupported Agent3 view_kind: {value}")


def _json_object(value: Any) -> dict[str, Any]:
    parsed = value if isinstance(value, dict) else json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[dict[str, Any]]:
    parsed = value if isinstance(value, list) else json.loads(str(value))
    return parsed if isinstance(parsed, list) else []
