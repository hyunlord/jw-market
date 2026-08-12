"""Approval-triggered CSD channel raw+stage publisher."""
from __future__ import annotations

import sys

from pipeline.scripts.ingest_hook import config, csd_channel_activation
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name
from pipeline.scripts.ingest_hook.job_runner import (
    _StageTracker,
    _drain_completion_queue,
    _emit_completion_signal,
    _mark_complete_after_required_stages,
    _measure_publish_source_set,
    _publish_source_set_reason,
    _release_writer_lock_preserving_primary,
    _source_set_from_contract,
    _stamp,
)
from pipeline.scripts.ingest_hook.ledger import STATUS_PUBLISH_RUNNING, Ledger
from pipeline.scripts.ingest_hook.ubist_mart_activation import acquire_writer_lock


def _record_context_bridge(
    ledger: Ledger, identity: tuple[str, str, str], run_id: str
) -> None:
    stamp = _stamp()
    ledger.record_stage(
        *identity,
        run_id=run_id,
        seq=4,
        stage="context_bridge",
        status="complete",
        reason="activated CSD channel stage is the context source",
        started_at=stamp,
        finished_at=stamp,
        duration_ms=0,
    )
    print("[stage] context_bridge end rc=0")


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
    raw_plan = payload.get("csd_activation_plan")
    raw_evidence = payload.get("csd_candidate_evidence")
    if not isinstance(raw_plan, dict) or not isinstance(raw_evidence, dict):
        ledger.mark_failed(*identity, reason="CSD publish candidate payload is incomplete")
        print("result=failed reason=CSD publish candidate payload is incomplete", file=sys.stderr)
        return 1
    try:
        plan = csd_channel_activation.plan_from_payload(raw_plan)
        recorded = csd_channel_activation.evidence_from_payload(raw_evidence)
        mode = str(payload.get("mode") or "production")
        raw_schema, stage_schema = config.csd_channel_live_schemas(mode=mode)
        csd_channel_activation.validate_plan_scope(
            plan,
            expected_run_id=build_run_id,
            raw_schema=raw_schema,
            stage_schema=stage_schema,
        )
    except Exception as exc:
        reason = f"CSD publish payload rejected: {type(exc).__name__}: {exc}"
        ledger.mark_failed(*identity, reason=reason)
        print(f"result=failed reason={reason}", file=sys.stderr)
        return 1
    tracker = _StageTracker(ledger, identity, publish_run_id)
    connection = None
    lock_acquired = False
    failure_reason = None
    try:
        tracker.enter("mart_publish")
        connection = config.open_csd_channel_connection()
        # This lock is node-local. Global serialization remains the ledger
        # awaiting_approval->publish_running CAS checked above.
        acquire_writer_lock(
            connection,
            timeout_seconds=0,
            lock_name=csd_channel_activation.WRITER_LOCK_NAME,
        )
        lock_acquired = True
        current = csd_channel_activation.validate_candidate(connection, plan, recorded)
        if current.raw != recorded.raw or current.stage != recorded.stage:
            raise csd_channel_activation.CandidateValidationError(
                "CSD candidate fingerprint changed after approval"
            )
        publish_source_set = _measure_publish_source_set(
            category,
            _source_set_from_contract(payload),
        )
        verdict = csd_channel_activation.publish_candidate(connection, plan, current)
        if verdict is not csd_channel_activation.SwapVerdict.APPLIED:
            raise RuntimeError(f"CSD publish was not applied: {verdict}")
        published_at = _stamp()
        publish_execution = tracker.done(
            reason=_publish_source_set_reason(publish_source_set)
        )
        tracker.skip("refresh", "CSD channel API reads the activated stage table directly")
        _record_context_bridge(ledger, identity, publish_run_id)
        tracker.complete_from(
            "dashboard",
            publish_execution,
            reason=(
                "dashboard reads atomically activated CSD channel stage directly; "
                f"target_schema={stage_schema}; raw_rows={current.raw.row_count}; "
                f"stage_rows={current.stage.row_count}"
            ),
        )
        row_counts = {
            plan.raw.live.table: current.raw.row_count,
            plan.stage.live.table: current.stage.row_count,
        }
        completion_signal = _emit_completion_signal(
            ledger=ledger,
            tracker=tracker,
            identity=identity,
            run_id=publish_run_id,
            event="complete",
            mode=mode,
            rows_before=int(payload.get("rows_before") or 0),
            rows_after=current.raw.row_count,
            rows_loaded=int(payload.get("rows_loaded") or 0),
            periods=set(current.periods.complete_quarters),
            started_at=candidate.prepared_at,
            failure_reason=None,
            target_schema=stage_schema,
            published_at=published_at,
            affected_scope={"dimension": "atc4", "count": 0, "values": []},
            drain_queue=False,
        )
        _mark_complete_after_required_stages(
            ledger=ledger,
            identity=identity,
            run_ids=(build_run_id, publish_run_id),
            row_counts=row_counts,
        )
        if completion_signal is not None:
            _drain_completion_queue(completion_signal)
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
                lock_name=csd_channel_activation.WRITER_LOCK_NAME,
                primary_failure_reason=failure_reason,
            )
        if connection is not None:
            connection.close()
