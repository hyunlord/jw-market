"""Precompute dynamic analysis-level blocks without changing their calculation."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import os
import struct
import time
from typing import Any

from pymysql.err import OperationalError

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
    channel_profile_signature,
)
from pipeline.scripts.api.dynamic_market.runtime_cache import dynamic_response_cache
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters, DynamicMarketRequest
from pipeline.scripts.api.routes.dynamic_market import _build_general_dynamic_response


MAX_BATCH_ROWS = 10
MAX_BATCH_BYTES = 8 * 1024 * 1024
BUILD_VERSION = ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class BlockKey:
    view: str
    market_id: str
    source: str
    measure: str
    profile_sig: str = ""
    trim_mode: str = "full"
    focus_brand_key: str | None = None


@dataclass(frozen=True, slots=True)
class BlockPayload:
    view: str
    market_id: str
    source: str
    measure: str
    profile_sig: str
    trim_mode: str
    analysis_levels_json: str
    market_status_json: str
    payload_sha256: str
    source_epoch: str
    build_version: str
    payload_size: int

    @classmethod
    def from_sections(
        cls,
        *,
        view: str,
        market_id: str,
        source: str,
        measure: str,
        profile_sig: str = "",
        trim_mode: str = "full",
        analysis_levels: dict[str, Any],
        market_status: dict[str, Any],
        source_epoch: str,
        build_version: str,
    ) -> BlockPayload:
        if view == "strategic_ml" and market_id == "ml_011":
            data = analysis_levels.get("data") or {}
            if data.get("Class") != data.get("Class 2"):
                raise ValueError("ml_011 must be stored post-alias with generic Class equal to Class 2")
        levels_bytes = strict_json_bytes(analysis_levels)
        status_bytes = strict_json_bytes(market_status)
        return cls(
            view=view,
            market_id=market_id,
            source=source,
            measure=measure,
            profile_sig=profile_sig,
            trim_mode=trim_mode,
            analysis_levels_json=levels_bytes.decode("utf-8"),
            market_status_json=status_bytes.decode("utf-8"),
            payload_sha256=framed_payload_sha256(levels_bytes, status_bytes),
            source_epoch=source_epoch,
            build_version=build_version,
            payload_size=len(levels_bytes) + len(status_bytes),
        )

    @classmethod
    def for_test(cls, *, market_id: str, payload_size: int) -> BlockPayload:
        return cls("general", market_id, "UBIST", "sales", "", "full", "{}", "{}", "0" * 64, "e" * 64, "test", payload_size)


def strict_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def framed_payload_sha256(analysis_levels: bytes, market_status: bytes) -> str:
    framed = (
        struct.pack(">Q", len(analysis_levels))
        + analysis_levels
        + struct.pack(">Q", len(market_status))
        + market_status
    )
    import hashlib

    return hashlib.sha256(framed).hexdigest()


def profile_signature(channels: Sequence[str]) -> str:
    return channel_profile_signature(channels)


def batch_blocks(
    blocks: Iterable[BlockPayload],
    *,
    max_rows: int = MAX_BATCH_ROWS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> Iterator[list[BlockPayload]]:
    batch: list[BlockPayload] = []
    size = 0
    for block in blocks:
        if batch and (len(batch) >= max_rows or size + block.payload_size > max_bytes):
            yield batch
            batch = []
            size = 0
        batch.append(block)
        size += block.payload_size
    if batch:
        yield batch


def enumerate_base_keys() -> list[BlockKey]:
    keys = [
        BlockKey("general", str(row["market_id"]), api_source(str(row["source"])), str(row["measure"]))
        for row in db.fetch_all(
            f"""
            SELECT DISTINCT atc4_code AS market_id, source, measure
            FROM `{config.db_name}`.`mart_general_market_metric`
            ORDER BY atc4_code, source, measure
            """,
            (),
        )
    ]
    for view, table, id_column in (
        ("strategic_ml", "mart_strategic_ml_market_metric", "ml_id"),
        ("strategic_cd", "mart_strategic_cd_market_metric", "cd_market_id"),
    ):
        keys.extend(
            BlockKey(view, str(row["market_id"]), api_source(str(row["source"])), str(row["measure"]))
            for row in db.fetch_all(
                f"""
                SELECT DISTINCT {id_column} AS market_id, source, measure
                FROM `{config.db_name}`.`{table}`
                ORDER BY {id_column}, source, measure
                """,
                (),
            )
        )
    return keys


def variant_keys(
    base_keys: Sequence[BlockKey],
    *,
    general_profiles: dict[tuple[str, str], list[tuple[str, str | None]]],
) -> list[BlockKey]:
    keys: list[BlockKey] = []
    for key in base_keys:
        if key.view == "general" and key.source == "UBIST":
            profiles = general_profiles[(key.market_id, key.measure)]
            keys.extend(
                BlockKey(key.view, key.market_id, key.source, key.measure, signature, "full", focus)
                for signature, focus in profiles
            )
        elif key.view.startswith("strategic_"):
            keys.append(BlockKey(key.view, key.market_id, key.source, key.measure, "", "full", None))
            keys.append(BlockKey(key.view, key.market_id, key.source, key.measure, "", "trim", None))
        else:
            keys.append(key)
    return keys


def enumerate_keys() -> list[BlockKey]:
    base_keys = enumerate_base_keys()
    return variant_keys(base_keys, general_profiles=discover_general_profiles(base_keys))


def discover_general_profiles(base_keys: Sequence[BlockKey]) -> dict[tuple[str, str], list[tuple[str, str | None]]]:
    ubist_keys = {(key.market_id, key.measure) for key in base_keys if key.view == "general" and key.source == "UBIST"}
    representatives: dict[tuple[str, str], set[str]] = {key: set() for key in ubist_keys}
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT atc4_code, measure, brand_key, brand_name
        FROM `{config.db_name}`.`mart_general_brand_metric`
        WHERE source = 'ubist'
        ORDER BY atc4_code, measure, brand_key
        """,
        (),
    )
    for row in rows:
        key = (str(row["atc4_code"]), str(row["measure"]))
        if key not in representatives:
            continue
        brand_key = str(row.get("brand_key") or "").strip()
        brand_name = str(row.get("brand_name") or "").strip()
        if get_display_brand(brand_key) is not None or get_display_brand(brand_name) is not None:
            representatives[key].add(brand_key or brand_name)

    profiles: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for market_id, measure in sorted(ubist_keys):
        by_signature: dict[str, str | None] = {}
        for focus in [None, *sorted(representatives[(market_id, measure)])]:
            data = _general_data(market_id=market_id, source="ubist", measure=measure, focus_brand_key=focus)
            status = data.get("analysis_level_market_status") or {}
            channels = status.get("channels") or []
            signature = profile_signature([str(channel) for channel in channels])
            by_signature.setdefault(signature, focus)
        profiles[(market_id, measure)] = sorted(by_signature.items())
    return profiles


def api_source(source: str) -> str:
    return "IQVIA" if source.lower() in {"iqvia", "iqvia_nsa", "nsa"} else "UBIST"


def mart_source(source: str) -> str:
    return "iqvia_nsa" if source == "IQVIA" else "ubist"


def build_block(key: BlockKey, *, source_epoch: str) -> BlockPayload:
    source = mart_source(key.source)
    if key.view == "general":
        data = _general_data(
            market_id=key.market_id,
            source=source,
            measure=key.measure,
            focus_brand_key=key.focus_brand_key,
        )
    else:
        focus = _strategic_focus(key, source=source)
        response = build_strategic_payload(
            mart_db=config.db_name,
            ml_id=key.market_id if key.view == "strategic_ml" else None,
            cd_market_id=key.market_id if key.view == "strategic_cd" else None,
            focus_brand_key=focus,
            source=source,
            measure=key.measure,
            analysis_level=DynamicMarketAnalysisLevelFilters(),
        )
        data = response.get("data") or {}
    levels = data.get("analysis_levels")
    status = data.get("analysis_level_market_status")
    if not isinstance(levels, dict) or not isinstance(status, dict):
        raise RuntimeError(f"analysis blocks missing for {key}")
    if key.trim_mode == "trim":
        from pipeline.scripts.etl.build_cache_cause import _trim_analysis_levels

        levels = _trim_analysis_levels(levels)
        status = _trim_analysis_levels(status)
    return BlockPayload.from_sections(
        view=key.view,
        market_id=key.market_id,
        source=key.source,
        measure=key.measure,
        profile_sig=key.profile_sig,
        trim_mode=key.trim_mode,
        analysis_levels=levels,
        market_status=status,
        source_epoch=source_epoch,
        build_version=BUILD_VERSION,
    )


def _general_data(*, market_id: str, source: str, measure: str, focus_brand_key: str | None) -> dict[str, Any]:
    request = DynamicMarketRequest.model_validate(
        {
            "view": "general",
            "source": source,
            "measure": measure,
            "filters": {"atc4": [market_id], "focus_brand_key": focus_brand_key},
        }
    )
    response = _build_general_dynamic_response(request)
    return ((response.get("result") or {}).get("data") or {})


def _strategic_focus(key: BlockKey, *, source: str) -> str:
    table = "mart_strategic_ml_brand_metric" if key.view == "strategic_ml" else "mart_strategic_cd_brand_metric"
    id_column = "ml_id" if key.view == "strategic_ml" else "cd_market_id"
    row = db.fetch_one(
        f"""
        SELECT brand_key
        FROM `{config.db_name}`.`{table}`
        WHERE {id_column} = %s AND source = %s AND measure = %s
        ORDER BY is_jw DESC, brand_key
        LIMIT 1
        """,
        (key.market_id, source, key.measure),
    )
    if not row:
        raise RuntimeError(f"strategic focus missing for {key}")
    return str(row["brand_key"])


UPSERT_SQL = """
INSERT INTO mart_analysis_level_block (
    view, market_id, source, measure, profile_sig, trim_mode, analysis_levels_json,
    analysis_level_market_status_json, payload_sha256, source_epoch,
    build_version, payload_size, built_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    analysis_levels_json = VALUES(analysis_levels_json),
    analysis_level_market_status_json = VALUES(analysis_level_market_status_json),
    payload_sha256 = VALUES(payload_sha256),
    source_epoch = VALUES(source_epoch),
    build_version = VALUES(build_version),
    payload_size = VALUES(payload_size),
    built_at = VALUES(built_at)
"""


def write_batch(batch: Sequence[BlockPayload]) -> None:
    params = [_upsert_params(block, built_at=datetime.now()) for block in batch]
    for attempt in range(4):
        try:
            with db.connect() as conn, conn.cursor() as cursor:
                cursor.executemany(UPSERT_SQL, params)
                conn.commit()
            return
        except OperationalError as exc:
            if exc.args[0] not in {1205, 1213} or attempt == 3:
                raise
            time.sleep(0.25 * (2**attempt))


def _upsert_params(block: BlockPayload, *, built_at: Any) -> tuple[Any, ...]:
    return (
        block.view,
        block.market_id,
        block.source,
        block.measure,
        block.profile_sig,
        block.trim_mode,
        block.analysis_levels_json,
        block.market_status_json,
        block.payload_sha256,
        block.source_epoch,
        block.build_version,
        block.payload_size,
        built_at,
    )


def source_epoch() -> str:
    return dynamic_response_cache._store.source_epoch()


def run_build() -> None:
    keys = sharded_keys(enumerate_keys())
    epoch = source_epoch()
    if os.environ.get("MALB_RESUME") == "1":
        existing = current_keys(source_epoch=epoch, build_version=BUILD_VERSION)
        keys = [key for key in keys if key not in existing]
    started = time.monotonic()
    built = 0
    transactions = 0
    pending: list[BlockPayload] = []
    pending_bytes = 0
    for key in keys:
        block = build_block(key, source_epoch=epoch)
        if pending and (len(pending) >= MAX_BATCH_ROWS or pending_bytes + block.payload_size > MAX_BATCH_BYTES):
            write_batch(pending)
            transactions += 1
            built += len(pending)
            print(json.dumps({"event": "batch", "built": built, "transactions": transactions}))
            pending = []
            pending_bytes = 0
        pending.append(block)
        pending_bytes += block.payload_size
    if pending:
        write_batch(pending)
        transactions += 1
        built += len(pending)
    print(json.dumps({"event": "complete", "built": built, "transactions": transactions, "seconds": time.monotonic() - started}))


def current_keys(*, source_epoch: str, build_version: str) -> set[BlockKey]:
    return {
        BlockKey(
            str(row["view"]), str(row["market_id"]), str(row["source"]), str(row["measure"]),
            str(row.get("profile_sig") or ""), str(row.get("trim_mode") or "full"), None,
        )
        for row in db.fetch_all(
            """
            SELECT view, market_id, source, measure, profile_sig, trim_mode
            FROM mart_analysis_level_block
            WHERE source_epoch = %s AND build_version = %s
            """,
            (source_epoch, build_version),
        )
    }


def run_parity() -> None:
    keys = sharded_keys(enumerate_keys())
    epoch = source_epoch()
    mismatches: list[dict[str, str]] = []
    for index, key in enumerate(keys, start=1):
        live = build_block(key, source_epoch=epoch)
        row = db.fetch_one(
            """
            SELECT payload_sha256 FROM mart_analysis_level_block
            WHERE view=%s AND market_id=%s AND source=%s AND measure=%s AND profile_sig=%s AND trim_mode=%s
            """,
            (key.view, key.market_id, key.source, key.measure, key.profile_sig, key.trim_mode),
        )
        stored = str((row or {}).get("payload_sha256") or "")
        if stored != live.payload_sha256:
            mismatches.append({"key": repr(key), "stored": stored, "live": live.payload_sha256})
        if index % 100 == 0:
            print(json.dumps({"event": "parity", "checked": index, "mismatches": len(mismatches)}))
    print(json.dumps({"event": "parity_complete", "checked": len(keys), "mismatches": mismatches}))
    if mismatches:
        raise SystemExit(1)


def sharded_keys(keys: list[BlockKey]) -> list[BlockKey]:
    if len(keys) != 3138:
        raise RuntimeError(f"expected 3138 keys, found {len(keys)}")
    count = int(os.environ.get("MALB_SHARD_COUNT", "1"))
    index = int(os.environ.get("MALB_SHARD_INDEX", "0"))
    if count < 1 or index < 0 or index >= count:
        raise RuntimeError(f"invalid shard {index}/{count}")
    return [key for position, key in enumerate(keys) if position % count == index]


if __name__ == "__main__":
    mode = os.environ.get("MALB_MODE", "build")
    if mode == "build":
        run_build()
    elif mode == "parity":
        run_parity()
    else:
        raise SystemExit(f"unsupported MALB_MODE: {mode}")
