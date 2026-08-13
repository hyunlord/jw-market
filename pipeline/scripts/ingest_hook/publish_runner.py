"""Entrypoint for the approval-triggered publish Job."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scripts.ingest_hook import config, post_success_cleanup
from pipeline.scripts.ingest_hook.category_map import ActivationKind, resolve_category
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name
from pipeline.scripts.ingest_hook.job_runner import (
    _StageTracker,
    _ledger_for_run,
    _mark_complete_after_required_stages,
    _measure_publish_source_set,
    _publish_source_set_reason,
    _release_writer_lock_preserving_primary,
    _run_commands_with_writer_lock,
    _source_set_from_contract,
)
from pipeline.scripts.ingest_hook.ledger import STATUS_PUBLISH_RUNNING, Ledger
from pipeline.scripts.ingest_hook.post_gate import SourceSnapshot, TableFingerprint, fingerprint_untouched_sources
from pipeline.scripts.ingest_hook.ubist_mart_activation import (
    WRITER_LOCK_NAME,
    CorpusCandidate,
    MartActivation,
    acquire_writer_lock,
    complete_recovery,
    fingerprint_build_tables,
    inventory_corpus,
    promote_candidate_corpus,
    publish_shadow,
    recover_incomplete_activations,
    require_corpus_manifest,
    shadow_lock_name,
    update_activation_journal,
    validate_shadow_publish,
)


def _snapshot(payload: list[dict]) -> SourceSnapshot:
    return SourceSnapshot(
        tuple(
            TableFingerprint(
                str(item["table"]),
                int(item["row_count"]),
                str(item["sample_sha256"]),
            )
            for item in payload
        )
    )


def _run_post_success_cleanup(
    *,
    connection,
    source: str,
    run_id: str,
    target_db: str,
) -> None:
    result = post_success_cleanup.run_post_success_cleanup(
        connection,
        serving_db=target_db,
        source=source,
        run_id=run_id,
    )
    print(
        "phase=post_success_cleanup status=complete "
        f"dry_run={str(result.dry_run).lower()} "
        f"targets={len(result.dropped)} "
        f"plan_path={result.plan_path} plan_sha256={result.plan_sha256}"
    )


def _verify_publish_integrity(
    payload: dict,
    corpus: CorpusCandidate,
    writer_conn,
    build_db: str,
) -> None:
    """Recheck the approved corpus and build schema while holding the writer lock."""
    recorded_corpus = payload.get("candidate_integrity")
    if not isinstance(recorded_corpus, dict):
        raise RuntimeError("publish candidate has no recorded corpus integrity")
    expected_corpus = (
        int(recorded_corpus.get("file_count", -1)),
        int(recorded_corpus.get("total_bytes", -1)),
        str(recorded_corpus.get("manifest_sha") or ""),
    )
    actual_corpus = inventory_corpus(corpus.candidate_root)
    if (
        actual_corpus.file_count,
        actual_corpus.total_bytes,
        actual_corpus.manifest_sha,
    ) != expected_corpus:
        raise RuntimeError("publish candidate corpus integrity changed after approval")

    recorded_build = payload.get("build_table_integrity")
    if not isinstance(recorded_build, list):
        raise RuntimeError("publish candidate has no recorded build-table integrity")
    expected_build = tuple(
        (
            str(item.get("table") or ""),
            int(item.get("row_count", -1)),
            int(item.get("crc_sum", -1)),
            int(item.get("crc_xor", -1)),
        )
        for item in recorded_build
        if isinstance(item, dict)
    )
    actual_build = tuple(
        (item.table, item.row_count, item.crc_sum, item.crc_xor)
        for item in fingerprint_build_tables(writer_conn, build_db)
    )
    if actual_build != expected_build:
        raise RuntimeError("publish build-table integrity changed after approval")


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
    ledger = _ledger_for_run(ledger, identity, build_run_id)
    if resolve_category(category).activation_kind is ActivationKind.CSD_CHANNEL:
        from pipeline.scripts.ingest_hook import csd_channel_publish_runner

        return csd_channel_publish_runner.run(
            ledger=ledger,
            epoch=epoch,
            category=category,
            manifest_sha=manifest_sha,
            build_run_id=build_run_id,
            publish_run_id=publish_run_id,
        )
    if resolve_category(category).activation_kind is ActivationKind.CSD_KEYWORD:
        from pipeline.scripts.ingest_hook import csd_keyword_publish_runner

        return csd_keyword_publish_runner.run(
            ledger=ledger,
            epoch=epoch,
            category=category,
            manifest_sha=manifest_sha,
            build_run_id=build_run_id,
            publish_run_id=publish_run_id,
        )
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
    expected_job_name = publish_job_name(category, manifest_sha, publish_run_id)
    if (
        entry.job_name != expected_job_name
        or candidate.publish_job_name != expected_job_name
    ):
        ledger.mark_failed(*identity, reason="publish Job identity mismatch")
        print("result=failed reason=publish Job identity mismatch", file=sys.stderr)
        return 1
    payload = candidate.payload
    mode = str(payload.get("mode") or "production")
    spec = resolve_category(category)
    activation = MartActivation(
        source_db=str(payload["source_db"]),
        target_db=str(payload["target_db"]),
        build_db=str(payload["build_db"]),
    )
    corpus = CorpusCandidate(
        Path(str(payload["live_root"])),
        Path(str(payload["candidate_root"])),
        Path(str(payload["backup_root"])),
    )
    activation_journal = Path(str(payload["activation_journal"]))
    tracker = _StageTracker(ledger, identity, publish_run_id)
    writer_conn = None
    writer_lock_name = (
        shadow_lock_name(activation.target_db)
        if mode == "shadow"
        else WRITER_LOCK_NAME
    )
    primary_failure_reason = None
    activation_mutated = False
    ledger_completed = False
    published_at = None
    try:
        tracker.enter("mart_publish")
        writer_db = activation.target_db if mode == "shadow" else None
        writer_conn = config.open_mart_connection(writer_db)
        acquire_writer_lock(writer_conn, timeout_seconds=0, lock_name=writer_lock_name)
        _verify_publish_integrity(payload, corpus, writer_conn, activation.build_db)
        expected_manifest_sha = str(payload["baseline_manifest_sha"])
        require_corpus_manifest(corpus.live_root, expected_manifest_sha)
        snapshot_conn = config.open_mart_connection(activation.source_db)
        try:
            current_live_snapshot = fingerprint_untouched_sources(
                snapshot_conn,
                touched_source="__jw_ingest_no_source__",
            )
        finally:
            snapshot_conn.close()
        if current_live_snapshot != _snapshot(list(payload["baseline_live_snapshot"])):
            raise RuntimeError("serving general mart changed while candidate was awaiting approval")
        update_activation_journal(activation_journal, "corpus_promotion_started")
        activation_mutated = True
        publish_source_set = _measure_publish_source_set(
            category,
            _source_set_from_contract(payload),
        )
        promote_candidate_corpus(corpus)
        update_activation_journal(activation_journal, "corpus_promoted")
        publish_shadow(
            writer_conn,
            activation,
            run_id=build_run_id,
            epoch=epoch,
            ingest_run_id=build_run_id,
            activation_journal=activation_journal,
            require_ledger_gate=mode != "shadow",
        )
        published_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        update_activation_journal(activation_journal, "mart_promoted")
        tracker.done(reason=_publish_source_set_reason(publish_source_set))
        tracker.enter("refresh")
        update_activation_journal(activation_journal, "refresh_started")
        if mode == "shadow":
            validate_shadow_publish(writer_conn, activation)
        else:
            _run_commands_with_writer_lock(
                "refresh",
                spec.refresh_argv,
                connection=writer_conn,
                lock_name=writer_lock_name,
            )
        update_activation_journal(activation_journal, "refresh_succeeded")
        refresh_execution = tracker.done()
        if mode == "shadow":
            tracker.skip("dashboard", "shadow validation does not update live dashboard serving")
        else:
            tracker.complete_from(
                "dashboard",
                refresh_execution,
                reason=(
                    "dashboard serving refresh completed; "
                    f"target_schema={activation.target_db}; "
                    f"refresh_argv={' '.join(spec.refresh_argv)}"
                ),
            )
            _run_post_success_cleanup(
                connection=writer_conn,
                source=category,
                run_id=build_run_id,
                target_db=activation.target_db,
            )
        row_counts = {
            str(key): int(value)
            for key, value in dict(payload.get("row_counts") or {}).items()
        }
        _mark_complete_after_required_stages(
            ledger=ledger,
            identity=identity,
            run_ids=(build_run_id, publish_run_id),
            row_counts=row_counts,
        )
        ledger_completed = True
        update_activation_journal(activation_journal, "ledger_complete")
        update_activation_journal(activation_journal, "complete")
        return 0
    except Exception as exc:  # fail closed across DB, filesystem, and refresh boundaries
        primary_failure_reason = f"{type(exc).__name__}: {exc}"
        if ledger_completed:
            print(
                f"result=committed_with_postcommit_error reason={primary_failure_reason}",
                file=sys.stderr,
            )
            return 1
        try:
            tracker.fail(primary_failure_reason)
        except Exception as tracker_exc:
            primary_failure_reason += (
                f"; stage_tracking_failed={type(tracker_exc).__name__}: {tracker_exc}"
            )
        if activation_mutated and writer_conn is not None:
            try:
                recovered = recover_incomplete_activations(
                    writer_conn,
                    output_root=activation_journal.parent,
                )
                if recovered:
                    if mode == "shadow":
                        validate_shadow_publish(writer_conn, activation)
                    else:
                        _run_commands_with_writer_lock(
                            "refresh-recovery",
                            spec.refresh_argv,
                            connection=writer_conn,
                            lock_name=writer_lock_name,
                        )
                    complete_recovery(recovered)
            except Exception as recovery_exc:
                primary_failure_reason += (
                    f"; recovery_failed={type(recovery_exc).__name__}: {recovery_exc}"
                )
        ledger.mark_failed(*identity, reason=primary_failure_reason)
        print(f"result=failed reason={primary_failure_reason}", file=sys.stderr)
        return 1
    finally:
        if writer_conn is not None:
            _release_writer_lock_preserving_primary(
                writer_conn,
                lock_name=writer_lock_name,
                primary_failure_reason=primary_failure_reason,
            )
            writer_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.scripts.ingest_hook.publish_runner"
    )
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--publish-run-id", required=True)
    args = parser.parse_args(argv)
    return run(
        ledger=config.open_configured_ledger(),
        epoch=args.epoch,
        category=args.category,
        manifest_sha=args.manifest_sha,
        build_run_id=args.build_run_id,
        publish_run_id=args.publish_run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
