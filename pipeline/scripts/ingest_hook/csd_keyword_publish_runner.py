"""Approval-triggered IQVIA CSD keyword raw+stage publisher."""
from __future__ import annotations

import sys

from pipeline.scripts.ingest_hook import config, csd_keyword_activation
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name
from pipeline.scripts.ingest_hook.job_runner import (
    _StageTracker,
    _emit_completion_signal,
    _release_writer_lock_preserving_primary,
    _stamp,
)
from pipeline.scripts.ingest_hook.ledger import STATUS_PUBLISH_RUNNING, Ledger
from pipeline.scripts.ingest_hook.ubist_mart_activation import acquire_writer_lock


def _record_downstream(ledger: Ledger, identity: tuple[str, str, str], run_id: str) -> None:
    for seq, stage in ((4, "context_bridge"), (5, "dashboard")):
        stamp = _stamp()
        ledger.record_stage(
            *identity,
            run_id=run_id,
            seq=seq,
            stage=stage,
            status="complete",
            reason="live stage table is queryable after atomic keyword publish",
            started_at=stamp,
            finished_at=stamp,
            duration_ms=0,
        )
        print(f"[stage] {stage} end rc=0")


def run(
    *,
    ledger: Ledger,
    epoch: str,
    category: str,
    manifest_sha: str,
    build_run_id: str,
    publish_run_id: str,
) -> int:
    identity = (epoch, category, manifest_sha)
    entry = ledger.status(*identity)
    if entry is None:
        print("result=failed reason=unknown publish identity", file=sys.stderr)
        return 2
    if entry.status != STATUS_PUBLISH_RUNNING:
        print(
            f"result=failed reason=publish requires {STATUS_PUBLISH_RUNNING}, got {entry.status}",
            file=sys.stderr,
        )
        return 1
    candidate = ledger.prepared_candidate(*identity)
    if candidate is None or candidate.build_run_id != build_run_id:
        ledger.mark_failed(*identity, reason="publish candidate identity mismatch")
        print("result=failed reason=publish candidate identity mismatch", file=sys.stderr)
        return 1
    expected_job = publish_job_name(category, manifest_sha, publish_run_id)
    if entry.job_name != expected_job or candidate.publish_job_name != expected_job:
        ledger.mark_failed(*identity, reason="publish Job identity mismatch")
        print("result=failed reason=publish Job identity mismatch", file=sys.stderr)
        return 1

    payload = candidate.payload
    raw_plan = payload.get("keyword_activation_plan")
    raw_evidence = payload.get("keyword_candidate_evidence")
    if not isinstance(raw_plan, dict) or not isinstance(raw_evidence, dict):
        ledger.mark_failed(*identity, reason="keyword publish candidate payload is incomplete")
        print("result=failed reason=keyword publish candidate payload is incomplete", file=sys.stderr)
        return 1
    try:
        plan = csd_keyword_activation.plan_from_payload(raw_plan)
        recorded = csd_keyword_activation.evidence_from_payload(raw_evidence)
        if plan.run_id != build_run_id:
            raise csd_keyword_activation.CandidateValidationError(
                "keyword activation run identity mismatch"
            )
    except Exception as exc:
        reason = f"keyword publish payload rejected: {type(exc).__name__}: {exc}"
        ledger.mark_failed(*identity, reason=reason)
        print(f"result=failed reason={reason}", file=sys.stderr)
        return 1

    tracker = _StageTracker(ledger, identity, publish_run_id)
    connection = None
    lock_acquired = False
    failure_reason = None
    try:
        tracker.enter("mart_publish")
        connection = config.open_mart_connection()
        acquire_writer_lock(
            connection,
            timeout_seconds=0,
            lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
        )
        lock_acquired = True
        current = csd_keyword_activation.validate_candidate(connection, plan)
        if current != recorded:
            raise csd_keyword_activation.CandidateValidationError(
                "keyword candidate evidence changed after approval"
            )
        csd_keyword_activation.require_publish_scope(connection, plan)
        csd_keyword_activation.publish_candidate(connection, plan)
        tracker.done()
        _record_downstream(ledger, identity, publish_run_id)
        ledger.mark_complete(
            *identity,
            row_counts={
                plan.raw.live.table: current.raw_rows,
                plan.stage.live.table: current.stage_rows,
            },
        )
        _emit_completion_signal(
            ledger=ledger,
            tracker=tracker,
            identity=identity,
            run_id=publish_run_id,
            event="complete",
            mode=str(payload.get("mode") or "production"),
            rows_before=int(payload.get("rows_before") or 0),
            rows_after=current.raw_rows,
            rows_loaded=int(payload.get("rows_loaded") or 0),
            periods={current.min_period, current.max_period},
            started_at=candidate.prepared_at,
            failure_reason=None,
        )
        return 0
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"
        tracker.fail(failure_reason)
        ledger.mark_failed(*identity, reason=failure_reason)
        print(f"result=failed reason={failure_reason}", file=sys.stderr)
        return 1
    finally:
        if connection is not None and lock_acquired:
            _release_writer_lock_preserving_primary(
                connection,
                lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
                primary_failure_reason=failure_reason,
            )
        if connection is not None:
            connection.close()
