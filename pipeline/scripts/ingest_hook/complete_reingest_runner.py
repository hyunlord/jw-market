"""Mart-only complete reingest runner for already-complete raw submissions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scripts.deploy.mart_load_ops import (
    publish_table_group_atomically,
    restore_table_group_atomically,
)
from pipeline.scripts.deploy.mart_load_verify import table_exists
from pipeline.scripts.etl.ops_forecast_builder import (
    EXPECTED_BLOCKS,
    EXPECTED_HORIZONS,
)
from pipeline.scripts.etl.ops_forecast_store import (
    LIVE_BLOCK,
    LIVE_HORIZON,
    completion_gate,
)
from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation as iqvia_activation
from pipeline.scripts.ingest_hook import ubist_mart_activation
from pipeline.scripts.ingest_hook.contract import load_manifest, parse_manifest_bytes


MODE = "mart_from_existing_raw"
REQUEST_SOURCE = "complete_reingest_request"
STAGES = {
    "request_validate": 1,
    "mart_build": 2,
    "mart_publish": 3,
    "refresh": 4,
    "agent_refresh": 5,
    "agent3": 6,
    "agent2": 7,
    "dashboard": 8,
}
ACTOR = "complete_reingest_runner"


class CompleteReingestRejected(RuntimeError):
    """A complete reingest attempt failed a prerequisite gate."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    identity: tuple[str, str, str]
    run_id: str
    category: str
    request_id: str
    parent_run_id: str
    affected_scope: dict[str, object]
    scope_values: tuple[str, ...] | None
    period_scope: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Publication:
    target_db: str
    actions: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class PreparedMart:
    target_db: str
    build_db: str
    tables: tuple[str, ...]


def run(
    manifest_path: Path,
    *,
    request_id: str,
    run_id: str,
    ledger=None,
    expected_affected_scope: dict[str, object] | None = None,
    input_source=None,
) -> None:
    manifest, _local_manifest = _load_manifest_only(manifest_path, input_source=input_source)
    active_ledger = ledger or config.open_configured_ledger()
    context = _validate_request(
        active_ledger,
        identity=(manifest.epoch, manifest.category, manifest.manifest_sha),
        request_id=request_id,
        run_id=run_id,
    )
    if expected_affected_scope is not None and context.affected_scope != expected_affected_scope:
        raise CompleteReingestRejected("CLI affected_scope differs from persisted request")

    try:
        _record_stage(active_ledger, context, "request_validate", "running")
        _record_stage(active_ledger, context, "request_validate")
        match context.category:
            case "ubist":
                prepared = _reuse_ubist_parent_build(context, active_ledger)
            case "iqvia_nsa":
                prepared = _reuse_iqvia_parent_build(context, active_ledger)
            case unsupported:
                raise CompleteReingestRejected(
                    f"complete reingest mart-only mode is unsupported for {unsupported!r}"
                )

        publication, forecast_counts = _publish_existing_mart(
            context, active_ledger, prepared
        )
        _record_reused_downstream(
            active_ledger,
            context,
            publication=publication,
            forecast_counts=forecast_counts,
        )
        _record_terminal(
            active_ledger,
            context,
            "complete",
            "existing mart promoted; forecast reused; recomputation=0",
        )
    except Exception as exc:
        try:
            _record_terminal(active_ledger, context, "failed", _reason(exc))
        except Exception as terminal_exc:
            raise RuntimeError(
                f"{_reason(exc)}; failed terminal record also failed: "
                f"{_reason(terminal_exc)}"
            ) from exc
        raise


def _record_stage(
    ledger,
    context: RequestContext,
    stage: str,
    status: str = "complete",
    reason: str | None = None,
) -> None:
    stamp = _stamp()
    ledger.record_stage(
        *context.identity, run_id=context.run_id, seq=STAGES[stage], stage=stage,
        status=status, reason=reason, started_at=stamp, finished_at=stamp, duration_ms=0,
    )


def _validate_request(
    ledger,
    *,
    identity: tuple[str, str, str],
    request_id: str,
    run_id: str,
) -> RequestContext:
    parent = ledger.status(*identity)
    if parent is None or getattr(parent, "status", None) != "complete":
        raise CompleteReingestRejected("parent complete ingest_ledger row is required")
    lookup = getattr(ledger, "complete_reingest_request", None)
    transition = (
        lookup(*identity, request_id=request_id)
        if callable(lookup)
        else _request_from_transitions(ledger, identity, request_id)
    )
    if transition is None:
        raise CompleteReingestRejected("persisted complete_reingest_request transition is required")
    evidence = getattr(transition, "evidence", None)
    if (
        getattr(transition, "source", None) != REQUEST_SOURCE
        or getattr(transition, "previous_status", None) != "complete"
        or getattr(transition, "status", None) != "complete"
        or not isinstance(evidence, dict)
        or evidence.get("mode") != MODE
        or evidence.get("request_id") != request_id
        or evidence.get("run_id") != run_id
    ):
        raise CompleteReingestRejected("persisted complete_reingest_request contract mismatch")
    affected_scope = evidence.get("affected_scope")
    if not isinstance(affected_scope, dict):
        raise CompleteReingestRejected("affected_scope is required")
    category = identity[1]
    scope_values, period_scope = _validate_scope(category, affected_scope)
    parent_run_id = str(getattr(parent, "run_id", "") or "")
    if not parent_run_id:
        raise CompleteReingestRejected("parent complete run_id is required")
    return RequestContext(
        identity,
        run_id,
        category,
        request_id,
        parent_run_id,
        affected_scope,
        scope_values,
        period_scope,
    )


def _request_from_transitions(ledger, identity: tuple[str, str, str], request_id: str):
    matches = [
        transition
        for transition in ledger.status_transitions(*identity)
        if getattr(transition, "event_id", None) == request_id
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_scope(
    category: str, affected_scope: dict[str, object]
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    dimension = affected_scope.get("dimension")
    count = affected_scope.get("count")
    values = affected_scope.get("values")
    if not (
        isinstance(dimension, str)
        and isinstance(count, int)
        and isinstance(values, list)
    ):
        raise CompleteReingestRejected("affected_scope must contain dimension, count, and values")
    clean_values = tuple(value for value in values if isinstance(value, str) and value)
    if len(clean_values) != count or not clean_values:
        raise CompleteReingestRejected("affected_scope values must exactly match count")
    match category:
        case "ubist":
            if dimension == "source" and clean_values == ("ubist",):
                return None, None
            if dimension != "atc4":
                raise CompleteReingestRejected("UBIST complete reingest scope is invalid")
            periods = affected_scope.get("periods")
            clean_periods = (
                tuple(value for value in periods if isinstance(value, str) and value)
                if isinstance(periods, list)
                else ()
            )
            if not clean_periods:
                raise CompleteReingestRejected("UBIST affected_scope requires period scope")
            return clean_values, clean_periods
        case "iqvia_nsa":
            if dimension != "source" or clean_values != ("iqvia_nsa",):
                raise CompleteReingestRejected("IQVIA NSA complete reingest requires source scope")
            return clean_values, ()
        case "skeleton" | "mi_master" | "iqvia_csd_channel" | "iqvia_csd_keyword":
            raise CompleteReingestRejected(f"unsupported complete reingest category: {category}")
        case _:
            raise CompleteReingestRejected(f"unknown complete reingest category: {category}")


def _reuse_ubist_parent_build(context: RequestContext, ledger) -> PreparedMart:
    activation = ubist_mart_activation.from_env(run_id=context.parent_run_id)
    build_conn = None
    try:
        try:
            _record_stage(ledger, context, "mart_build", "running")
            build_conn = config.open_mart_connection(activation.build_db)
            ubist_mart_activation.fingerprint_build_tables(
                build_conn, activation.build_db
            )
        except Exception as exc:
            _record_stage(ledger, context, "mart_build", "failed", _reason(exc))
            raise
        _record_stage(
            ledger,
            context,
            "mart_build",
            reason=(
                "reused existing parent build without recomputation: "
                f"{activation.build_db}"
            ),
        )
        return PreparedMart(
            target_db=activation.target_db,
            build_db=activation.build_db,
            tables=ubist_mart_activation.NUMERIC_TABLES,
        )
    finally:
        if build_conn is not None:
            build_conn.close()


def _reuse_iqvia_parent_build(context: RequestContext, ledger) -> PreparedMart:
    activation = iqvia_activation.from_env(run_id=context.parent_run_id)
    build_conn = config.open_mart_connection(activation.build_db)
    try:
        try:
            _record_stage(ledger, context, "mart_build", "running")
            _require_existing_build_tables(
                build_conn,
                build_db=activation.build_db,
                tables=iqvia_activation.NSA_PUBLISH_TABLES,
            )
        except Exception as exc:
            _record_stage(ledger, context, "mart_build", "failed", _reason(exc))
            raise
        _record_stage(
            ledger,
            context,
            "mart_build",
            reason=(
                "reused existing parent build without recomputation: "
                f"{activation.build_db}"
            ),
        )
        return PreparedMart(
            target_db=activation.target_db,
            build_db=activation.build_db,
            tables=iqvia_activation.NSA_PUBLISH_TABLES,
        )
    finally:
        build_conn.close()


def _publish_existing_mart(
    context: RequestContext,
    ledger,
    prepared: PreparedMart,
) -> tuple[Publication, dict[str, int]]:
    writer_conn = config.open_mart_connection(prepared.target_db)
    lock_name = ubist_mart_activation.WRITER_LOCK_NAME
    lock_acquired = False
    primary_failure_reason: str | None = None
    try:
        ubist_mart_activation.acquire_writer_lock(
            writer_conn,
            timeout_seconds=0,
            lock_name=lock_name,
        )
        lock_acquired = True
        try:
            _record_stage(ledger, context, "mart_publish", "running")
            actions = _publish_table_group(
                writer_conn,
                build_db=prepared.build_db,
                target_db=prepared.target_db,
                run_id=context.run_id,
                tables=prepared.tables,
            )
        except Exception as exc:
            _record_stage(ledger, context, "mart_publish", "failed", _reason(exc))
            raise
        publication = Publication(prepared.target_db, tuple(actions))
        _record_stage(
            ledger,
            context,
            "mart_publish",
            reason=(
                "promoted existing parent build without recomputation; "
                f"rollback_anchor={_rollback_anchor(publication)}"
            ),
        )

        try:
            _record_stage(ledger, context, "refresh", "running")
            forecast_counts = _verify_existing_forecast(writer_conn)
        except Exception as exc:
            _record_stage(ledger, context, "refresh", "failed", _reason(exc))
            try:
                _restore_publication(
                    writer_conn,
                    publication=publication,
                    run_id=context.run_id,
                )
            except Exception as restore_exc:
                raise RuntimeError(
                    f"{_reason(exc)}; atomic mart restore failed: "
                    f"{_reason(restore_exc)}"
                ) from exc
            raise
        _record_stage(
            ledger,
            context,
            "refresh",
            reason="existing downstream artifacts verified; cache_rebuild=0",
        )
        return publication, forecast_counts
    except Exception as exc:
        primary_failure_reason = _reason(exc)
        raise
    finally:
        if lock_acquired:
            try:
                ubist_mart_activation.release_writer_lock(
                    writer_conn, lock_name=lock_name
                )
            except Exception as cleanup_exc:
                if primary_failure_reason is None:
                    raise
                print(
                    "cleanup=writer_lock_release_failed "
                    f"primary_preserved={primary_failure_reason} "
                    f"cleanup_reason={_reason(cleanup_exc)}",
                    file=sys.stderr,
                )
        writer_conn.close()


def _require_existing_build_tables(
    conn, *, build_db: str, tables: tuple[str, ...]
) -> None:
    missing = tuple(table for table in tables if not table_exists(conn, build_db, table))
    if missing:
        raise CompleteReingestRejected(
            "parent build artifacts are absent; recomputation is forbidden: "
            + ", ".join(f"{build_db}.{table}" for table in missing)
        )


def _verify_existing_forecast(connection) -> dict[str, int]:
    return completion_gate(
        connection,
        LIVE_BLOCK,
        LIVE_HORIZON,
        EXPECTED_BLOCKS,
        EXPECTED_HORIZONS,
    )


def _rollback_anchor(publication: Publication) -> str:
    anchors = tuple(
        f"{getattr(action, 'table', '<unknown>')}:{getattr(action, 'backup_table', '<missing>')}"
        for action in publication.actions
    )
    return ",".join(anchors) or "none"


def _record_reused_downstream(
    ledger,
    context: RequestContext,
    *,
    publication: Publication,
    forecast_counts: dict[str, int],
) -> None:
    _record_stage(
        ledger,
        context,
        "agent_refresh",
        reason=(
            "reused existing live forecast without recomputation; "
            f"blocks={forecast_counts['blocks']} "
            f"horizons={forecast_counts['horizons']} "
            f"bad_simulation={forecast_counts['bad_simulation']}"
        ),
    )
    reason = (
        "existing output retained; LLM=0 cache_rebuild=0; "
        f"rollback_anchor={_rollback_anchor(publication)}"
    )
    for stage in ("agent3", "agent2", "dashboard"):
        _record_stage(ledger, context, stage, reason=reason)


def _publish_table_group(
    conn,
    *,
    build_db: str,
    target_db: str,
    run_id: str,
    tables: tuple[str, ...],
):
    return publish_table_group_atomically(
        conn,
        build_db=build_db,
        target_db=target_db,
        run_id=run_id,
        tables=tables,
    )


def _restore_publication(
    connection,
    *,
    publication: Publication,
    run_id: str,
) -> None:
    restore_table_group_atomically(
        connection,
        target_db=publication.target_db,
        actions=publication.actions,
        run_id=run_id,
    )


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _record_terminal(ledger, context: RequestContext, status: str, reason: str) -> None:
    recorder = getattr(ledger, "record_complete_reingest_terminal", None)
    if callable(recorder):
        recorder(
            *context.identity,
            request_id=context.request_id,
            run_id=context.run_id,
            status=status,
            reason=reason,
            actor=ACTOR,
            job_name=os.environ.get("HOSTNAME"),
            affected_scope=context.affected_scope,
        )


def _parse_scope_json(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompleteReingestRejected("affected_scope JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise CompleteReingestRejected("affected_scope JSON must be an object")
    return payload


def _load_manifest_only(manifest_path: Path, *, input_source=None):
    if input_source is None:
        return load_manifest(manifest_path), manifest_path
    key = str(manifest_path).lstrip("/")
    payload = input_source.read(key)
    workdir = Path(tempfile.mkdtemp(prefix="complete_reingest_manifest_"))
    local_manifest = workdir / key
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(payload)
    return parse_manifest_bytes(payload, manifest_path=key), local_manifest


def _require_cli_identity(args: argparse.Namespace, *, input_source=None) -> Path:
    manifest_path = Path(args.manifest)
    manifest, local_manifest = _load_manifest_only(manifest_path, input_source=input_source)
    actual = (manifest.epoch, manifest.category, manifest.manifest_sha)
    expected = (args.epoch, args.category, args.manifest_sha)
    if actual != expected:
        raise CompleteReingestRejected(
            f"CLI identity differs from manifest: expected={expected} actual={actual}"
        )
    return local_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--affected-scope-json", required=True)
    args = parser.parse_args(argv)
    input_source = config.open_input_source()
    manifest_path = _require_cli_identity(args, input_source=input_source)
    run(
        manifest_path,
        request_id=args.request_id,
        run_id=args.run_id,
        expected_affected_scope=_parse_scope_json(args.affected_scope_json),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
