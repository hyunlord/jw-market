"""Refresh the semantic row-topic bridge after a Keyword stage replacement."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
import uuid

from pipeline.scripts.analysis.brand_activity.auto_topic.backfill_stage_occurrence import (
    backfill_current_generation,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_db import prepare_run
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_execute import PROMPT_VERSION
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_runner import (
    EMPIRICAL_USD_PER_CALL,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_cli import (
    DEFAULT_BATCH_SIZE,
    MAX_WAVE_CALLS,
    SemanticWavePlan,
    TopicOccurrenceSet,
    _execute_selected_wave,
    build_wave_plan,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_db import (
    cas_active_release,
    load_bridge_occurrences,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_runner import (
    SemanticOccurrence,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import (
    validated_stage_schema,
)


POINTER_NAME = "brand_activity_keyword"
SEMANTIC_CONTRACT = "semantic_v1"
SOURCE_TABLE = "km_keyword_event_stage"
RELEASE_TABLE = "row_topic_taxonomy_release_v1"
MANIFEST_TABLE = "row_topic_taxonomy_release_manifest_v1"
ACTIVE_TABLE = "row_topic_taxonomy_active_release_v1"
STATUS_TABLE = "row_topic_assignment_status_semantic_v1"
RUN_TABLE = "row_topic_assignment_run_semantic_v1"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    scope_id: str
    topic_set_version: str
    assignment_contract: str
    stage_generation_id: str | None


@dataclass(frozen=True, slots=True)
class ActiveReleaseSnapshot:
    pointer_name: str
    release_id: str
    generation: int
    stage_generation_id: str
    manifest: tuple[ManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class KeywordSemanticRefreshResult:
    stage_generation_id: str
    bridge_inserted_rows: int
    bridge_reused_rows: int
    bridge_generation_rows: int
    reused_semantic_identities: int
    new_semantic_identities: int
    planned_calls: int
    llm_calls: int
    estimated_usd: float
    active_release_id: str
    pointer_generation: int
    pointer_changed: bool


def refresh_keyword_semantic(
    connection: Any,
    *,
    schema: str,
    ingest_run_id: str,
    created_by: str,
    max_calls: int = MAX_WAVE_CALLS,
    max_usd: float = 12.0,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> KeywordSemanticRefreshResult:
    """Backfill the current stage, classify only new identities, and CAS the pointer."""
    safe_schema = validated_stage_schema(schema)
    snapshot = _active_release_snapshot(connection, schema=safe_schema)
    expected_rows = _stage_row_count(connection, schema=safe_schema)
    bridge = backfill_current_generation(
        connection,
        schema=safe_schema,
        batch_size=1000,
        expected_rows=expected_rows,
    )
    stage_generation = str(bridge["stage_generation_id"])
    if _snapshot_already_current(snapshot, stage_generation):
        return _result(
            bridge,
            reused=0,
            new=0,
            planned_calls=0,
            llm_calls=0,
            estimated_usd=0.0,
            release_id=snapshot.release_id,
            generation=snapshot.generation,
            pointer_changed=False,
        )

    missing_work, reused = _missing_semantic_work(
        connection,
        schema=safe_schema,
        stage_generation_id=stage_generation,
        manifest=snapshot.manifest,
    )
    plan = build_wave_plan(
        missing_work,
        prompt_version=PROMPT_VERSION,
        batch_size=batch_size,
        max_calls=max_calls,
    )
    if plan.estimated_usd > max_usd:
        raise RuntimeError(
            "semantic incremental cost cap exceeded: "
            f"estimated_usd={plan.estimated_usd:.6f}, max_usd={max_usd:.6f}"
        )
    release_id = _release_id(snapshot.release_id, stage_generation)
    if plan.total_calls == 0:
        print(
            json.dumps(
                {
                    "event": "keyword_semantic_zero_case",
                    "new_semantic_identities": 0,
                    "planned_calls": 0,
                    "actual_llm_calls": 0,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        llm_calls = 0
    else:
        llm_calls = _execute_missing_work(
            connection,
            schema=safe_schema,
            work=missing_work,
            plan=plan,
            ingest_run_id=ingest_run_id,
            release_id=release_id,
            created_by=created_by,
            max_calls=max_calls,
            max_usd=max_usd,
        )
    active_release_id, pointer_generation = _publish_release(
        connection,
        schema=safe_schema,
        snapshot=snapshot,
        stage_generation_id=stage_generation,
        release_id=release_id,
        created_by=created_by,
    )
    return _result(
        bridge,
        reused=reused,
        new=sum(len(item.occurrences) for item in missing_work),
        planned_calls=plan.total_calls,
        llm_calls=llm_calls,
        estimated_usd=llm_calls * EMPIRICAL_USD_PER_CALL,
        release_id=active_release_id,
        generation=pointer_generation,
        pointer_changed=True,
    )


def _active_release_snapshot(connection: Any, *, schema: str) -> ActiveReleaseSnapshot:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT p.pointer_name, p.active_release_id, p.generation,
                       r.stage_generation_id
                FROM `{schema}`.`{ACTIVE_TABLE}` p
                JOIN `{schema}`.`{RELEASE_TABLE}` r ON r.release_id=p.active_release_id
                WHERE p.pointer_name=%s
            """,
            (POINTER_NAME,),
        )
        pointer = cursor.fetchone()
        if pointer is None:
            raise RuntimeError(f"active semantic pointer is unavailable: {POINTER_NAME}")
        release_id = str(pointer["active_release_id"])
        cursor.execute(
            f"""
                SELECT scope_id, topic_set_version, assignment_contract,
                       stage_generation_id
                FROM `{schema}`.`{MANIFEST_TABLE}`
                WHERE release_id=%s
                ORDER BY scope_id
            """,
            (release_id,),
        )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"active release manifest is empty: {release_id}")
    return ActiveReleaseSnapshot(
        pointer_name=str(pointer["pointer_name"]),
        release_id=release_id,
        generation=int(pointer["generation"]),
        stage_generation_id=str(pointer["stage_generation_id"]),
        manifest=tuple(
            ManifestEntry(
                scope_id=str(row["scope_id"]),
                topic_set_version=str(row["topic_set_version"]),
                assignment_contract=str(row["assignment_contract"]),
                stage_generation_id=(
                    str(row["stage_generation_id"])
                    if row["stage_generation_id"] is not None
                    else None
                ),
            )
            for row in rows
        ),
    )


def _stage_row_count(connection: Any, *, schema: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{schema}`.`{SOURCE_TABLE}`")
        return int(cursor.fetchone()["row_count"])


def _missing_semantic_work(
    connection: Any,
    *,
    schema: str,
    stage_generation_id: str,
    manifest: tuple[ManifestEntry, ...],
) -> tuple[tuple[TopicOccurrenceSet, ...], int]:
    scopes_by_version: dict[str, list[str]] = defaultdict(list)
    for entry in manifest:
        if entry.assignment_contract == SEMANTIC_CONTRACT:
            scopes_by_version[entry.topic_set_version].append(entry.scope_id)
    if not scopes_by_version:
        raise RuntimeError("active release has no semantic scopes")

    work: list[TopicOccurrenceSet] = []
    reused_identities = 0
    for version in sorted(scopes_by_version):
        scopes = tuple(sorted(scopes_by_version[version]))
        occurrences = load_bridge_occurrences(
            connection,
            schema=schema,
            stage_generation_id=stage_generation_id,
            topic_set_version=version,
            scope_ids=scopes,
        )
        existing = _existing_identities(
            connection,
            schema=schema,
            topic_set_version=version,
            scope_ids=scopes,
        )
        representatives: dict[tuple[str, str, str], SemanticOccurrence] = {}
        seen: set[tuple[str, str, str]] = set()
        for occurrence in occurrences:
            identity = (occurrence.semantic_event_key_v1, occurrence.scope_id, version)
            if identity in seen:
                continue
            seen.add(identity)
            if identity in existing:
                reused_identities += 1
                continue
            representatives[identity] = occurrence
        work.append(
            TopicOccurrenceSet(
                topic_set_version=version,
                occurrences=tuple(representatives[key] for key in sorted(representatives)),
            )
        )
    return tuple(work), reused_identities


def _existing_identities(
    connection: Any,
    *,
    schema: str,
    topic_set_version: str,
    scope_ids: tuple[str, ...],
) -> set[tuple[str, str, str]]:
    placeholders = ",".join("%s" for _ in scope_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT semantic_event_key_v1, scope_id, topic_set_version
                FROM `{schema}`.`{STATUS_TABLE}`
                WHERE topic_set_version=%s AND scope_id IN ({placeholders})
                ORDER BY scope_id, semantic_event_key_v1
            """,
            (topic_set_version, *scope_ids),
        )
        rows = cursor.fetchall()
    return {
        (
            str(row["semantic_event_key_v1"]),
            str(row["scope_id"]),
            str(row["topic_set_version"]),
        )
        for row in rows
    }


def _execute_missing_work(
    connection: Any,
    *,
    schema: str,
    work: tuple[TopicOccurrenceSet, ...],
    plan: SemanticWavePlan,
    ingest_run_id: str,
    release_id: str,
    created_by: str,
    max_calls: int,
    max_usd: float,
) -> int:
    if not os.environ.get("GENOS_BEARER_TOKEN"):
        raise RuntimeError("GENOS_BEARER_TOKEN is required for new semantic identities")
    prepared = {
        item.topic_set_version: prepare_run(
            connection,
            schema=schema,
            topic_set_version=item.topic_set_version,
        )
        for item in work
        if item.occurrences
    }
    semantic_run_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"jw:keyword-semantic:{ingest_run_id}:{plan.waves[0].batches[0].batch.batch_id}",
        )
    )
    args = argparse.Namespace(
        run_id=semantic_run_id,
        release_id=release_id,
        stage_generation_id=next(
            occurrence.stage_generation_id
            for item in work
            for occurrence in item.occurrences
        ),
        prompt_version=PROMPT_VERSION,
        created_by=created_by,
        max_calls=max_calls,
        base_url=os.environ.get("GENOS_BASE_URL", "https://jwai-dev.jwhealthcare.com"),
        serving_id=os.environ.get("ROW_TOPIC_SERVING_ID", "163"),
        failed_response_log=Path(
            os.environ.get(
                "ROW_TOPIC_FAILED_RESPONSE_LOG",
                "/tmp/row_topic_semantic_failed_responses.jsonl",
            )
        ),
        stop_on_response_parse=False,
    )
    for wave in plan.waves:
        used_before = _semantic_run_calls(connection, schema=schema, run_id=semantic_run_id)
        remaining_calls = math.floor(max_usd / EMPIRICAL_USD_PER_CALL) - used_before
        if remaining_calls <= 0:
            raise RuntimeError(
                f"semantic incremental cost cap reached: calls_used={used_before}, max_usd={max_usd:.6f}"
            )
        args.max_calls = min(max_calls, remaining_calls)
        rc = _execute_selected_wave(
            connection,
            args=args,
            schema=schema,
            plan=plan,
            wave=wave,
            prepared=prepared,
        )
        if rc != 0:
            raise RuntimeError(f"semantic incremental wave failed: wave={wave.wave_no}, rc={rc}")
        used_after = _semantic_run_calls(connection, schema=schema, run_id=semantic_run_id)
        if used_after * EMPIRICAL_USD_PER_CALL > max_usd:
            raise RuntimeError(
                f"semantic incremental cost cap exceeded: calls_used={used_after}, max_usd={max_usd:.6f}"
            )
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT calls_used, status FROM `{schema}`.`{RUN_TABLE}` WHERE run_id=%s",
            (semantic_run_id,),
        )
        row = cursor.fetchone()
    if row is None or str(row["status"]) != "complete":
        raise RuntimeError(f"semantic incremental run did not complete: {semantic_run_id}")
    return int(row["calls_used"])


def _semantic_run_calls(connection: Any, *, schema: str, run_id: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT COALESCE(SUM(calls_used), 0) AS calls_used
                FROM `{schema}`.`row_topic_assignment_batch_semantic_v1`
                WHERE run_id=%s
            """,
            (run_id,),
        )
        return int(cursor.fetchone()["calls_used"])


def _publish_release(
    connection: Any,
    *,
    schema: str,
    snapshot: ActiveReleaseSnapshot,
    stage_generation_id: str,
    release_id: str,
    created_by: str,
) -> tuple[str, int]:
    manifest = tuple(
        ManifestEntry(
            scope_id=entry.scope_id,
            topic_set_version=entry.topic_set_version,
            assignment_contract=entry.assignment_contract,
            stage_generation_id=(
                stage_generation_id
                if entry.assignment_contract == SEMANTIC_CONTRACT
                else entry.stage_generation_id
            ),
        )
        for entry in snapshot.manifest
    )
    manifest_sha = _manifest_sha(manifest)
    now = _utc_naive()
    semantic_count = sum(entry.assignment_contract == SEMANTIC_CONTRACT for entry in manifest)
    legacy_count = len(manifest) - semantic_count
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT manifest_sha256, stage_generation_id, status FROM `{schema}`.`{RELEASE_TABLE}` WHERE release_id=%s",
                (release_id,),
            )
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(
                    f"""
                        INSERT INTO `{schema}`.`{RELEASE_TABLE}`
                          (release_id, manifest_sha256, stage_generation_id, status,
                           expected_scope_count, semantic_scope_count, legacy_scope_count,
                           created_at, created_by, ready_at)
                        VALUES (%s,%s,%s,'ready',%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        release_id,
                        manifest_sha,
                        stage_generation_id,
                        len(manifest),
                        semantic_count,
                        legacy_count,
                        now,
                        created_by,
                        now,
                    ),
                )
                cursor.executemany(
                    f"""
                        INSERT INTO `{schema}`.`{MANIFEST_TABLE}`
                          (release_id, scope_id, topic_set_version, assignment_contract,
                           stage_generation_id, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        (
                            release_id,
                            entry.scope_id,
                            entry.topic_set_version,
                            entry.assignment_contract,
                            entry.stage_generation_id,
                            now,
                        )
                        for entry in manifest
                    ],
                )
            elif (
                str(existing["manifest_sha256"]),
                str(existing["stage_generation_id"]),
                str(existing["status"]),
            ) != (manifest_sha, stage_generation_id, "ready"):
                raise RuntimeError("deterministic semantic release identity has different content")
        cas_active_release(
            connection,
            schema=schema,
            pointer_name=snapshot.pointer_name,
            expected_generation=snapshot.generation,
            expected_active_release_id=snapshot.release_id,
            new_release_id=release_id,
            actor=created_by,
            now_utc_naive=now,
        )
    except Exception:
        connection.rollback()
        raise
    return release_id, snapshot.generation + 1


def _snapshot_already_current(snapshot: ActiveReleaseSnapshot, generation: str) -> bool:
    return snapshot.stage_generation_id == generation and all(
        entry.assignment_contract != SEMANTIC_CONTRACT
        or entry.stage_generation_id == generation
        for entry in snapshot.manifest
    )


def _manifest_sha(manifest: tuple[ManifestEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(manifest, key=lambda item: item.scope_id):
        digest.update(
            "\x1f".join(
                (
                    entry.scope_id,
                    entry.topic_set_version,
                    entry.assignment_contract,
                    entry.stage_generation_id or "",
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _release_id(previous_release_id: str, stage_generation_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"jw:row-topic-release:{previous_release_id}:{stage_generation_id}",
        )
    )


def _utc_naive() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="microseconds"
    )


def _result(
    bridge: dict[str, object],
    *,
    reused: int,
    new: int,
    planned_calls: int,
    llm_calls: int,
    estimated_usd: float,
    release_id: str,
    generation: int,
    pointer_changed: bool,
) -> KeywordSemanticRefreshResult:
    return KeywordSemanticRefreshResult(
        stage_generation_id=str(bridge["stage_generation_id"]),
        bridge_inserted_rows=int(bridge["inserted_rows"]),
        bridge_reused_rows=int(bridge["reused_rows"]),
        bridge_generation_rows=int(bridge["generation_rows"]),
        reused_semantic_identities=reused,
        new_semantic_identities=new,
        planned_calls=planned_calls,
        llm_calls=llm_calls,
        estimated_usd=estimated_usd,
        active_release_id=release_id,
        pointer_generation=generation,
        pointer_changed=pointer_changed,
    )
