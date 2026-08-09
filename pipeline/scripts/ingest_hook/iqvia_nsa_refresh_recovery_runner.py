"""Recover a rolled-back IQVIA NSA publish without rebuilding its mart."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation as activation
from pipeline.scripts.ingest_hook import iqvia_nsa_publication as publication
from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook import ubist_mart_activation as ubist_activation
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.ledger import STATUS_FAILED, Ledger


def _inventory_rows(inventory_json: str) -> dict[str, int]:
    try:
        inventory = json.loads(inventory_json)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("NSA recovery inventory JSON is invalid") from exc
    if not isinstance(inventory, list) or not inventory:
        raise RuntimeError("NSA recovery inventory must contain at least one file")
    rows: dict[str, int] = {}
    for item in inventory:
        if not isinstance(item, dict):
            raise RuntimeError("NSA recovery inventory contains a non-object item")
        path = str(item.get("path") or "")
        try:
            count = int(item["rows"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("NSA recovery inventory contains an invalid row count") from exc
        if not path or path in rows or count < 0:
            raise RuntimeError("NSA recovery inventory contains an invalid file identity")
        rows[path] = count
    return rows


def _validate_failed_run(
    ledger: Ledger,
    identity: tuple[str, str, str],
    *,
    failed_run_id: str,
) -> None:
    entry = ledger.status(*identity)
    if entry is None or entry.run_id != failed_run_id:
        raise RuntimeError("exact failed NSA ledger identity was not found")
    if entry.status != STATUS_FAILED:
        raise RuntimeError(f"NSA refresh recovery requires failed status, got {entry.status}")
    if ledger.prepared_candidate(*identity) is not None:
        raise RuntimeError("NSA direct publication must not have a prepared candidate")
    stages = {
        event.stage: event
        for event in ledger.stage_events(*identity)
        if event.run_id == failed_run_id
    }
    if stages.get("mart_publish") is None or stages["mart_publish"].status != "complete":
        raise RuntimeError("NSA refresh recovery requires a completed mart_publish stage")
    if stages.get("refresh") is None or stages["refresh"].status != "failed":
        raise RuntimeError("NSA refresh recovery requires a failed refresh stage")


def _close_interrupted_refresh_stage(
    ledger: Ledger,
    identity: tuple[str, str, str],
    *,
    interrupted_run_id: str,
    superseding_run_id: str,
) -> None:
    matches = [
        event
        for event in ledger.stage_events(*identity)
        if event.run_id == interrupted_run_id
        and event.seq == 8
        and event.stage == "refresh"
    ]
    if len(matches) != 1:
        raise RuntimeError("exact interrupted refresh stage was not found")
    interrupted = matches[0]
    if interrupted.status == "failed" and interrupted.finished_at is not None:
        return
    if interrupted.status != "running" or interrupted.finished_at is not None:
        raise RuntimeError(
            "interrupted refresh stage is not an open running stage: "
            f"status={interrupted.status} finished_at={interrupted.finished_at}"
        )

    reason = f"superseded by recovery run {superseding_run_id}"
    ledger.record_stage(
        *identity,
        run_id=interrupted_run_id,
        seq=8,
        stage="refresh",
        status="failed",
        reason=reason,
        finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    persisted = [
        event
        for event in ledger.stage_events(*identity)
        if event.run_id == interrupted_run_id
        and event.seq == 8
        and event.stage == "refresh"
    ]
    if (
        len(persisted) != 1
        or persisted[0].status != "failed"
        or persisted[0].finished_at is None
        or persisted[0].reason != reason
    ):
        raise RuntimeError("interrupted refresh stage did not close")


def recover(
    *,
    ledger: Ledger,
    writer_conn: Any,
    activation_config: activation.NsaMartActivation,
    identity: tuple[str, str, str],
    failed_run_id: str,
    recovery_run_id: str,
    refresh_argv: tuple[str, ...],
    promoted_recovery_run_id: str | None = None,
) -> None:
    _validate_failed_run(ledger, identity, failed_run_id=failed_run_id)
    if not refresh_argv:
        raise RuntimeError("NSA refresh command is empty")
    evidence = publication.read_rolled_back_publication(
        writer_conn,
        activation_config,
        run_id=failed_run_id,
    )
    if (evidence.epoch, evidence.run_id) != (identity[0], failed_run_id):
        raise RuntimeError("rolled-back publication identity does not match the ledger")
    actual_inventory_sha256 = hashlib.sha256(
        evidence.inventory_json.encode("utf-8")
    ).hexdigest()
    if actual_inventory_sha256 != evidence.inventory_sha256:
        raise RuntimeError("rolled-back publication inventory SHA256 mismatch")
    row_counts = _inventory_rows(evidence.inventory_json)
    periods = {evidence.window_start, evidence.window_end}
    if promoted_recovery_run_id is not None:
        _close_interrupted_refresh_stage(
            ledger,
            identity,
            interrupted_run_id=promoted_recovery_run_id,
            superseding_run_id=recovery_run_id,
        )
    tracker = job_runner._StageTracker(ledger, identity, recovery_run_id)
    lock_name = ubist_activation.WRITER_LOCK_NAME
    actions: tuple[Any, ...] = ()
    provenance_recovered = False
    lock_acquired = False
    primary: BaseException | None = None
    try:
        ubist_activation.acquire_writer_lock(
            writer_conn,
            timeout_seconds=0,
            lock_name=lock_name,
        )
        lock_acquired = True
        ubist_activation.require_writer_lock_owner(writer_conn, lock_name=lock_name)
        if promoted_recovery_run_id is None:
            actions = activation.promote_failed_publication_atomically(
                writer_conn,
                activation_config,
                failed_run_id=failed_run_id,
                recovery_run_id=recovery_run_id,
            )
        else:
            actions = activation.resume_failed_publication_actions(
                writer_conn,
                activation_config,
                failed_run_id=failed_run_id,
                promoted_recovery_run_id=promoted_recovery_run_id,
            )
        tracker.enter("refresh")
        try:
            job_runner._run_commands_with_writer_lock(
                "refresh",
                refresh_argv,
                connection=writer_conn,
                lock_name=lock_name,
            )
        except Exception as exc:
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise
        tracker.done()
        publication.mark_publication_recovered(
            writer_conn,
            activation_config,
            run_id=failed_run_id,
            publication_epoch=evidence.mart_publication_epoch,
            inventory_sha256=evidence.inventory_sha256,
        )
        provenance_recovered = True
        ledger.mark_complete(*identity, row_counts=row_counts)
        now = datetime.now(timezone.utc).isoformat()
        job_runner._emit_completion_signal(
            ledger=ledger,
            tracker=tracker,
            identity=identity,
            run_id=recovery_run_id,
            event="complete",
            mode="real",
            rows_before=0,
            rows_after=sum(row_counts.values()),
            rows_loaded=sum(row_counts.values()),
            periods=periods,
            started_at=now,
            failure_reason=None,
            target_schema=activation_config.target_db,
            published_at=now,
            affected_scope=job_runner._completion_affected_scope(identity[1]),
        )
    except BaseException as exc:
        primary = exc
        if actions:
            try:
                activation.restore_failed_publication_atomically(
                    writer_conn,
                    activation_config,
                    actions=actions,
                    failed_run_id=failed_run_id,
                )
                job_runner._run_commands_with_writer_lock(
                    "refresh-restored-serving",
                    refresh_argv,
                    connection=writer_conn,
                    lock_name=lock_name,
                )
                if provenance_recovered:
                    publication.mark_publication_recovery_rolled_back(
                        writer_conn,
                        activation_config,
                        run_id=failed_run_id,
                        publication_epoch=evidence.mart_publication_epoch,
                        inventory_sha256=evidence.inventory_sha256,
                    )
            except Exception as restore_exc:
                raise RuntimeError(
                    f"NSA recovery failed ({type(exc).__name__}: {exc}); "
                    f"serving restore also failed ({type(restore_exc).__name__}: {restore_exc})"
                ) from exc
        raise
    finally:
        if lock_acquired:
            try:
                ubist_activation.release_writer_lock(writer_conn, lock_name=lock_name)
            except Exception as release_exc:
                if primary is None:
                    raise
                print(
                    "cleanup=writer_lock_release_failed "
                    f"primary_preserved={type(primary).__name__}: {primary} "
                    f"cleanup_reason={type(release_exc).__name__}: {release_exc}",
                    file=sys.stderr,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.scripts.ingest_hook.iqvia_nsa_refresh_recovery_runner"
    )
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True, choices=("iqvia_nsa",))
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--failed-run-id", required=True)
    parser.add_argument(
        "--recovery-run-id",
        default=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
    )
    parser.add_argument(
        "--promoted-recovery-run-id",
        help="resume refresh after this recovery run already promoted the tables",
    )
    args = parser.parse_args(argv)
    identity = (args.epoch, args.category, args.manifest_sha)
    ledger = config.open_configured_ledger()
    activation_config = activation.from_env(run_id=args.failed_run_id)
    writer_conn = config.open_mart_connection(activation_config.target_db)
    try:
        recover(
            ledger=ledger,
            writer_conn=writer_conn,
            activation_config=activation_config,
            identity=identity,
            failed_run_id=args.failed_run_id,
            recovery_run_id=args.recovery_run_id,
            promoted_recovery_run_id=args.promoted_recovery_run_id,
            refresh_argv=resolve_category("iqvia_nsa").refresh_argv,
        )
    except Exception as exc:
        print(f"result=failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        writer_conn.close()
    print(
        "result=complete mode=refresh_recovery "
        f"epoch={args.epoch} category={args.category} run_id={args.failed_run_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
