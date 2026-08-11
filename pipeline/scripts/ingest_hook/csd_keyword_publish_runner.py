"""Approval-triggered IQVIA CSD keyword raw+stage publisher."""
from __future__ import annotations

import sys
import time
from typing import Any, Callable

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
from pipeline.scripts.deploy.brand_activity_307.row_topic_monthly_wrapper import (
    RowTopicRunResult,
    run_for_ingest,
)


def _candidate_period_scope(connection: Any, plan: Any) -> dict[str, object]:
    ref = plan.stage.candidate
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT DISTINCT period_ym FROM `{ref.schema}`.`{ref.table}` "
            "WHERE period_ym IS NOT NULL AND period_ym <> '' ORDER BY period_ym"
        )
        rows = cursor.fetchall()
    values = sorted({str(row["period_ym"] if isinstance(row, dict) else row[0]) for row in rows})
    return {"dimension": "period_ym", "count": len(values), "values": values}


def _record_topic_assignment(
    *,
    ledger: Ledger,
    identity: tuple[str, str, str],
    run_id: str,
    affected_scope: dict[str, object],
    runner: Callable[..., RowTopicRunResult] = run_for_ingest,
) -> None:
    started_at = _stamp()
    duration_ms: int | None = None
    status = "failed_nonfatal"
    try:
        enabled = config.keyword_topic_assign_enabled()
    except Exception as exc:
        enabled = None
        reason = f"배정 실패: {type(exc).__name__}: {exc}"
    if enabled is False:
        status = "skipped"
        reason = "배정 비활성 (KEYWORD_TOPIC_ASSIGN_ENABLED=false)"
    elif enabled is True:
        started = time.monotonic()
        try:
            result = runner(
                affected_scope=affected_scope,
                category=identity[1],
                epoch=identity[0],
                manifest_sha=identity[2],
                run_id=run_id,
            )
            reason = (
                "배정 대상 없음"
                if result.pending_rows == 0
                else f"배정 {result.inserts}건 생성 · LLM 호출 {result.calls}회"
            )
            status = "complete"
        except Exception as exc:  # Assignment is intentionally non-fatal to atomic ingest.
            reason = f"배정 실패: {type(exc).__name__}: {exc}"
            print(f"[stage] topic_extraction nonfatal reason={reason}", file=sys.stderr)
        duration_ms = max(1, round((time.monotonic() - started) * 1000))
    finished_at = _stamp()
    ledger.record_stage(
        *identity,
        run_id=run_id,
        seq=4,
        stage="topic_extraction",
        status=status,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )
    print("[stage] topic_extraction end rc=0")


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
        raw_schema, stage_schema = config.csd_keyword_live_schemas()
        plan = csd_keyword_activation.plan_from_payload(
            raw_plan,
            raw_schema=raw_schema,
            stage_schema=stage_schema,
        )
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
    writer_connection = None
    activation_connection = None
    lock_acquired = False
    failure_reason = None
    try:
        tracker.enter("mart_publish")
        activation_connection = config.open_csd_channel_connection()
        acquire_writer_lock(
            activation_connection,
            timeout_seconds=0,
            lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
        )
        lock_acquired = True
        writer_connection = config.open_mart_connection()
        current = csd_keyword_activation.validate_candidate(writer_connection, plan)
        if current != recorded:
            raise csd_keyword_activation.CandidateValidationError(
                "keyword candidate evidence changed after approval"
            )
        affected_scope = _candidate_period_scope(writer_connection, plan)
        csd_keyword_activation.publish_candidate(
            activation_connection, plan, current
        )
        published_at = _stamp()
        publish_execution = tracker.done()
        _record_topic_assignment(
            ledger=ledger,
            identity=identity,
            run_id=publish_run_id,
            affected_scope=affected_scope,
        )
        tracker.complete_from(
            "dashboard",
            publish_execution,
            reason=(
                "dashboard reads atomically activated CSD keyword stage directly; "
                f"target_schema={stage_schema}; raw_rows={current.raw_rows}; "
                f"stage_rows={current.stage_rows}"
            ),
        )
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
            target_schema=stage_schema,
            published_at=published_at,
            affected_scope={"dimension": "atc4", "count": 0, "values": []},
        )
        return 0
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"
        tracker.fail(failure_reason)
        ledger.mark_failed(*identity, reason=failure_reason)
        print(f"result=failed reason={failure_reason}", file=sys.stderr)
        return 1
    finally:
        if activation_connection is not None and lock_acquired:
            _release_writer_lock_preserving_primary(
                activation_connection,
                lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
                primary_failure_reason=failure_reason,
            )
        if writer_connection is not None:
            writer_connection.close()
        if activation_connection is not None:
            activation_connection.close()
