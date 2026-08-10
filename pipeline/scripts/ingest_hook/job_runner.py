"""Entrypoint executed inside the ingest Job.

Order is enforced in code (STOP ③ — no load without G3):
  1. contract parse (fail-closed on unknown category)
  2. G3 structural validation
  3. load phase        (rehearsal: CSV -> sqlite staging; real: pipeline.etl.run)
  4. isolated mart build from the candidate corpus
  5. POST-GATE (Σ, manifest row coverage, untouched-source fingerprint)
  6. atomic corpus + serving mart publish
  7. downstream refresh (real: pipeline.orchestrator --mode incremental)
  8. ledger complete
Any post-gate failure marks the ledger row gate_failed and blocks promotion;
other failures mark it failed. Both exit non-zero;
nothing is promoted (rehearsal writes staging only; the real loaders keep
their own staging->promotion discipline).

Rehearsal mode (INGEST_REHEARSAL_ROOT or --rehearsal-root) exists so the whole
chain can be exercised with zero production contact — G-1/G-2 evidence.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.category_map import (
    ActivationKind,
    CategorySpec,
    UnknownCategoryError,
    resolve_category,
)
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from pipeline.scripts.ingest_hook.ledger import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PUBLISH_RUNNING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Ledger,
)


class _ReingestAttemptLedger:
    """Keep a rerun append-only while exposing the normal runner contract."""

    def __init__(self, ledger: Ledger, attempt: object) -> None:
        self._ledger = ledger
        self._attempt = attempt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ledger, name)

    def status(self, epoch: str, category: str, manifest_sha: str):
        entry = self._ledger.status(epoch, category, manifest_sha)
        if entry is not None:
            candidate = self._ledger.prepared_candidate(epoch, category, manifest_sha)
            if candidate is not None and candidate.build_run_id == self._attempt.run_id:
                return replace(
                    entry,
                    status=(
                        STATUS_PUBLISH_RUNNING
                        if candidate.publish_job_name
                        else STATUS_AWAITING_APPROVAL
                    ),
                    run_id=self._attempt.run_id,
                    job_name=candidate.publish_job_name or self._attempt.job_name,
                )
            return replace(
                entry,
                status=STATUS_RUNNING,
                run_id=self._attempt.run_id,
                job_name=self._attempt.job_name,
            )
        return entry

    def _terminal(self, status: str, reason: str) -> None:
        self._ledger.record_complete_reingest_terminal(
            self._attempt.epoch,
            self._attempt.category,
            self._attempt.manifest_sha,
            request_id=self._attempt.request_id,
            run_id=self._attempt.run_id,
            status=status,
            reason=reason,
            actor="job_runner",
            job_name=self._attempt.job_name,
            affected_scope=self._attempt.affected_scope,
        )

    def mark_complete(self, *_identity: str, row_counts: dict[str, int]) -> None:
        del row_counts
        self._terminal(STATUS_COMPLETE, "normal ingest pipeline complete")

    def mark_failed(self, *_identity: str, reason: str) -> None:
        self._terminal(STATUS_FAILED, reason)

    def mark_gate_failed(self, *_identity: str, reason: str) -> None:
        self._terminal(STATUS_FAILED, reason)

    def mark_awaiting_approval(
        self,
        *identity: str,
        run_id: str,
        candidate: dict,
        prepared_at: str,
        expires_at: str,
    ) -> None:
        self._ledger.prepare_complete_reingest_candidate(
            *identity,
            request_id=self._attempt.request_id,
            run_id=run_id,
            candidate=candidate,
            prepared_at=prepared_at,
            expires_at=expires_at,
        )

    def mark_publish_running(
        self,
        *identity: str,
        build_run_id: str,
        publish_job_name: str,
        approved_by: str,
        approved_at: str,
    ) -> bool:
        return self._ledger.mark_complete_reingest_publish_running(
            *identity,
            request_id=self._attempt.request_id,
            build_run_id=build_run_id,
            publish_job_name=publish_job_name,
            approved_by=approved_by,
            approved_at=approved_at,
        )

    def restore_awaiting_approval_after_submit_failure(
        self,
        *identity: str,
        build_run_id: str,
        publish_job_name: str,
    ) -> bool:
        return self._ledger.restore_complete_reingest_awaiting_approval(
            *identity,
            request_id=self._attempt.request_id,
            build_run_id=build_run_id,
            publish_job_name=publish_job_name,
        )

    def mark_publish_candidate_expired(
        self,
        *_identity: str,
        build_run_id: str,
        actor: str,
    ) -> bool:
        del build_run_id, actor
        self._terminal(STATUS_FAILED, "publish candidate expired before approval")
        return True


def _ledger_for_run(
    ledger: Ledger,
    identity: tuple[str, str, str],
    run_id: str,
) -> Ledger | _ReingestAttemptLedger:
    lookup = getattr(ledger, "complete_reingest_attempts", None)
    if not callable(lookup):
        return ledger
    attempts = lookup(category=identity[1])
    attempt = next(
        (
            item
            for item in attempts
            if (item.epoch, item.category, item.manifest_sha, item.run_id)
            == (*identity, run_id)
            and item.status == STATUS_RUNNING
        ),
        None,
    )
    return _ReingestAttemptLedger(ledger, attempt) if attempt is not None else ledger
from pipeline.scripts.ingest_hook.post_gate import (
    PostGateError,
    SigmaEvidence,
    SourceSnapshot,
    TableFingerprint,
    fingerprint_untouched_sources,
    run_post_gates,
    sample_existing_periods,
    staging_row_count,
)
from pipeline.scripts.ingest_hook.sigma_gate import SigmaGateError, check_staging
from pipeline.scripts.ingest_hook.source_inventory import (
    DEFAULT_INVENTORY_ROOT,
    ScanOutcome,
    run_full_scan,
)
from pipeline.scripts.ingest_hook.source_inventory_runtime import (
    latest_successful_snapshot,
    load_scan_policy,
)


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _StageTracker:
    """Records each job_runner stage to ingest_stage_event + stdout markers.

    Purely observational (S-4): the ledger recorder swallows its own DB errors, so
    a recording failure never breaks the load. Markers use the R-1 form
    ``[stage] <name> start(i/N)`` / ``end rc=N`` so a silent kill (OOM/eviction)
    leaves the failing stage as the last marker in the durable log.
    Rows accumulate per run_id (S-3): a retry passes a new run_id, never overwriting.
    """

    STAGES = (
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
    )

    def __init__(self, ledger: Ledger, identity: tuple[str, str, str], run_id: str):
        self._ledger = ledger
        self._identity = identity
        self._run_id = run_id
        self._seq = {name: index + 1 for index, name in enumerate(self.STAGES)}
        self._n = len(self.STAGES)
        self._current: str | None = None
        self._t0: float | None = None

    def _record(self, name: str, status: str, *, reason: str | None = None,
                started: str | None = None, finished: str | None = None, duration_ms: int | None = None) -> None:
        self._ledger.record_stage(
            *self._identity, run_id=self._run_id, seq=self._seq[name], stage=name,
            status=status, reason=reason, started_at=started, finished_at=finished, duration_ms=duration_ms,
        )

    def enter(self, name: str) -> None:
        self._current = name
        self._t0 = time.monotonic()
        self._record(name, "running", started=_stamp())
        print(f"[stage] {name} start({self._seq[name]}/{self._n})")

    def done(self, rc: int = 0, *, reason: str | None = None) -> None:
        if self._current is None:
            return
        name, dur = self._current, self._elapsed_ms()
        self._record(name, "complete", reason=reason, finished=_stamp(), duration_ms=dur)
        print(f"[stage] {name} end rc={rc}")
        self._current = None
        self._t0 = None

    def complete(self, name: str, reason: str | None = None) -> None:
        """Record a stage that ran inside a helper (load_verify inside _real_load;
        sigma inside run_post_gates) as complete without a separate enter/exit."""
        stamp = _stamp()
        self._record(name, "complete", reason=reason, started=stamp, finished=stamp, duration_ms=0)
        print(f"[stage] {name} end rc=0")

    def skip(self, name: str, reason: str) -> None:
        stamp = _stamp()
        self._record(name, "skipped", reason=reason, started=stamp, finished=stamp, duration_ms=0)
        print(f"[stage] {name} skipped reason={reason}")

    def record_failure(self, name: str, reason: str) -> None:
        """Record a helper-owned stage failure even when it is not in flight."""
        stamp = _stamp()
        self._record(
            name,
            "failed",
            reason=reason,
            started=stamp,
            finished=stamp,
            duration_ms=0,
        )
        print(f"[stage] {name} end rc=1 reason={reason}")

    def fail(self, reason: str) -> None:
        """Mark the in-flight stage failed (called from run()'s except handlers)."""
        if self._current is None:
            return
        name, dur = self._current, self._elapsed_ms()
        self._record(name, "failed", reason=reason, finished=_stamp(), duration_ms=dur)
        print(f"[stage] {name} end rc=1")
        self._current = None
        self._t0 = None

    def _elapsed_ms(self) -> int | None:
        return int((time.monotonic() - self._t0) * 1000) if self._t0 is not None else None


_SOURCE_STAGE_CONTRACTS: dict[str, tuple[str, ...]] = {
    "ubist": (
        "job_submit",
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
        "agent_refresh",
        "agent3",
        "agent2",
        "dashboard",
    ),
    "iqvia_nsa": (
        "job_submit",
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
        "agent_refresh",
        "agent3",
        "agent2",
        "dashboard",
    ),
    "iqvia_csd_channel": (
        "job_submit",
        "g3",
        "load",
        "load_verify",
        "mart_publish",
        "context_bridge",
        "dashboard",
        "signal",
    ),
    "iqvia_csd_keyword": (
        "job_submit",
        "g3",
        "load",
        "load_verify",
        "post_gate",
        "mart_publish",
        "topic_extraction",
        "dashboard",
        "signal",
    ),
}


def expected_stages(spec: CategorySpec) -> list[dict[str, str | int | bool]]:
    """Return the deterministic stage skeleton for one category."""
    source_stages = _SOURCE_STAGE_CONTRACTS.get(spec.key)
    if source_stages is not None:
        return [
            {"stage": stage, "seq": seq, "applicable": True}
            for seq, stage in enumerate(source_stages, start=1)
        ]

    supports_mart = spec.activation_kind in {
        ActivationKind.UBIST_NUMERIC,
        ActivationKind.IQVIA_NSA,
    }
    supports_source_activation = spec.activation_kind in {
        ActivationKind.CSD_CHANNEL,
        ActivationKind.CSD_KEYWORD,
    }
    applicability = {
        "g3": True,
        "load": bool(spec.load_argv),
        "load_verify": bool(spec.load_verify),
        "mart_build": supports_mart,
        "sigma": supports_mart and bool(spec.sigma_source),
        "post_gate": (supports_mart and bool(spec.sigma_source)) or supports_source_activation,
        "mart_publish": supports_mart or supports_source_activation,
        "refresh": bool(spec.refresh_argv) and spec.production_load_supported,
        "signal": True,
    }
    return [
        {
            "stage": stage,
            "seq": seq,
            "applicable": applicability[stage],
        }
        for seq, stage in enumerate(_StageTracker.STAGES, start=1)
    ]


def _rehearsal_load(manifest, input_root: Path, rehearsal_root: Path) -> str:
    """Load submission CSVs into an isolated sqlite staging table."""
    rehearsal_root.mkdir(parents=True, exist_ok=True)
    staging_db = rehearsal_root / "staging.db"
    table = f"ingest_staging_{manifest.category}"
    conn = sqlite3.connect(str(staging_db))
    try:
        conn.execute(f"DROP TABLE IF EXISTS {table}")  # staging is per-run scratch
        conn.execute(
            f"CREATE TABLE {table} (period TEXT, level TEXT, brand TEXT, value REAL)"
        )
        for entry in manifest.files:
            path = input_root / entry.path
            if path.suffix.lower() != ".csv":
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    conn.execute(
                        f"INSERT INTO {table} (period, level, brand, value) VALUES (?, ?, ?, ?)",
                        (
                            (row.get("period") or "").strip(),
                            (row.get("level") or "").strip(),
                            (row.get("brand") or "").strip(),
                            float(row.get("value") or 0.0),
                        ),
                    )
        conn.commit()
    finally:
        conn.close()
    return table


def _run_commands(label: str, argv: tuple[str, ...]) -> None:
    if not argv:
        return
    result = subprocess.run(argv, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{label} command failed rc={result.returncode}: {' '.join(argv)}")


def _run_commands_with_writer_lock(
    label: str,
    argv: tuple[str, ...],
    *,
    connection,
    lock_name: str,
    heartbeat_seconds: float = 30.0,
) -> None:
    """Run a long command while keeping its session-owned writer lock alive."""
    if not argv:
        return
    from pipeline.scripts.ingest_hook import ubist_mart_activation

    ubist_mart_activation.require_writer_lock_owner(
        connection, lock_name=lock_name
    )
    process = subprocess.Popen(argv)
    while True:
        try:
            returncode = process.wait(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            try:
                ubist_mart_activation.require_writer_lock_owner(
                    connection, lock_name=lock_name
                )
            except Exception as lock_exc:  # fail closed at the subprocess boundary
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RuntimeError(
                    f"{label} aborted because writer lock ownership was lost: "
                    f"{lock_exc}"
                ) from lock_exc

    command_error = (
        RuntimeError(
            f"{label} command failed rc={returncode}: {' '.join(argv)}"
        )
        if returncode != 0
        else None
    )
    try:
        ubist_mart_activation.require_writer_lock_owner(
            connection, lock_name=lock_name
        )
    except Exception as lock_exc:  # preserve a command failure if both occurred
        if command_error is not None:
            raise RuntimeError(
                f"{command_error}; writer lock verification also failed: {lock_exc}"
            ) from command_error
        raise
    if command_error is not None:
        raise command_error


def _release_writer_lock_preserving_primary(
    connection,
    *,
    lock_name: str,
    primary_failure_reason: str | None,
) -> None:
    from pipeline.scripts.ingest_hook import ubist_mart_activation

    try:
        ubist_mart_activation.release_writer_lock(
            connection, lock_name=lock_name
        )
    except Exception as cleanup_exc:
        if primary_failure_reason is None:
            raise
        print(
            "cleanup=writer_lock_release_failed "
            f"primary_preserved={primary_failure_reason} "
            f"cleanup_reason={type(cleanup_exc).__name__}: {cleanup_exc}",
            file=sys.stderr,
        )


def _run_recovery_refresh(
    *,
    tracker: _StageTracker,
    argv: tuple[str, ...],
    connection,
    lock_name: str,
) -> None:
    tracker.enter("refresh")
    try:
        _run_commands_with_writer_lock(
            "refresh",
            argv,
            connection=connection,
            lock_name=lock_name,
        )
    except Exception as exc:
        tracker.fail(f"{type(exc).__name__}: {exc}")
        raise
    tracker.done()


def _recovery_tracker(
    ledger: Ledger,
    identity: tuple[str, str, str],
    *,
    run_id: str,
    phase: str,
) -> _StageTracker:
    suffix = f":{phase}-recovery"
    recovery_run_id = f"{run_id[: 64 - len(suffix)]}{suffix}"
    return _StageTracker(ledger, identity, recovery_run_id)


_EMPTY_UBIST_MANIFEST = '{"schema_version": "1.0", "partitions": []}'
_ENV_PUBLISH_CANDIDATE_TTL_SECONDS = "INGEST_PUBLISH_CANDIDATE_TTL_SECONDS"
_DEFAULT_PUBLISH_CANDIDATE_TTL_SECONDS = 86400


def _publish_candidate_ttl_seconds() -> int:
    raw = os.environ.get(_ENV_PUBLISH_CANDIDATE_TTL_SECONDS, "").strip()
    if not raw:
        return _DEFAULT_PUBLISH_CANDIDATE_TTL_SECONDS
    value = int(raw)
    if value < 1:
        raise RuntimeError(f"{_ENV_PUBLISH_CANDIDATE_TTL_SECONDS} must be a positive integer")
    return value


def _seed_empty_manifest(target_dir: Path, verify_kind: str | None) -> None:
    """A fresh staging target needs a baseline manifest so the incremental loader
    treats every uploaded period as new (the loader reads _manifest.json first)."""
    if verify_kind == "ubist_parquet_manifest":
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest = target_dir / "_manifest.json"
        if not manifest.exists():
            manifest.write_text(_EMPTY_UBIST_MANIFEST, encoding="utf-8")


def _epoch_rows(target_dir: Path, epoch: str) -> int:
    manifest_path = target_dir / "_manifest.json"
    if not manifest_path.is_file():
        return 0
    import json

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return sum(
        int(item.get("row_count") or 0)
        for item in payload.get("partitions", [])
        if str(item.get("period_yyyymm")) == epoch
    )


def _real_load(
    manifest,
    spec,
    input_root: Path,
    *,
    target_dir_override: Path | None = None,
    source_files: tuple[Path, ...] | None = None,
    target_db_override: str | None = None,
) -> dict:
    """Wire the materialized upload into the loader, run it, and prove the epoch
    landed (M-2). Returns {target_dir, epoch_rows, staging_verify}.

    Fail-closed rules:
      * a category with a load_argv but no load_input_flag is UNWIRED — refuse
        to run it in real mode (it would load unrelated defaults = silent failure).
      * the epoch must appear in the loader's own output with rows > 0.
    """
    from pipeline.scripts.ingest_hook.load_verify import (
        LoadVerifyError,
        verify_epoch_loaded,
        verify_table_load,
    )

    if not spec.load_argv:
        return {
            "target_dir": None,
            "epoch_rows": None,
            "staging_verify": None,
            "load_verify_complete": False,
            "load_verify_warning": None,
        }  # e.g. skeleton

    if not spec.load_input_flag:
        raise RuntimeError(
            f"category {manifest.category!r} has a load command but no upload wiring "
            "(load_input_flag); refusing to load unrelated defaults (silent-failure guard)"
        )

    target_root, staging_verify = config.load_output_root()
    mode = str(config.load_mode())
    activation_capability = config.source_activation_enabled(manifest.category, mode=mode)
    if not staging_verify and not spec.production_load_supported and not activation_capability:
        raise RuntimeError(
            f"category {manifest.category!r} table loader is isolated-staging only; "
            "refusing production completion until a separate production activation gate"
        )
    if target_dir_override is not None:
        target_dir = target_dir_override
    elif manifest.category == "ubist" and not staging_verify:
        # The general mart reader consumes <ubist-root>/year=*/month=*/data.parquet.
        # Do not insert the submission epoch between that root and its partitions.
        target_dir = target_root / manifest.category
    else:
        target_dir = target_root / manifest.category / manifest.epoch
    if staging_verify:
        target_dir /= manifest.manifest_sha
    rows_before = _epoch_rows(target_dir, manifest.epoch)
    _seed_empty_manifest(target_dir, spec.load_verify)

    read_files = (
        [str(path.resolve()) for path in source_files]
        if source_files is not None
        else [str((input_root / entry.path).resolve()) for entry in manifest.files]
    )
    source_batches = [read_files] if spec.load_batch_files else [[source] for source in read_files]
    previous_target_db = os.environ.get(config.ENV_LOAD_STAGING_DB)
    if target_db_override is not None:
        os.environ[config.ENV_LOAD_STAGING_DB] = target_db_override
    try:
        for sources in source_batches:
            argv = list(spec.load_argv)
            for source in sources:
                argv.extend([spec.load_input_flag, source])
            if spec.load_target_flag:
                argv.extend([spec.load_target_flag, str(target_dir)])
            if spec.load_epoch_flag:
                argv.extend([spec.load_epoch_flag, manifest.epoch])
            print(
                f"phase=load files={len(sources)} target={target_dir} "
                f"staging_verify={staging_verify}"
            )
            _run_commands("load", tuple(argv))
    finally:
        if target_db_override is not None:
            if previous_target_db is None:
                os.environ.pop(config.ENV_LOAD_STAGING_DB, None)
            else:
                os.environ[config.ENV_LOAD_STAGING_DB] = previous_target_db

    # M-2: the uploaded epoch must be present in the loader's output.
    epoch_rows = None
    rows_loaded = 0
    load_verify_warning = None
    if spec.load_verify:
        try:
            if spec.load_verify == "table_manifest":
                evidence = verify_table_load(target_dir, manifest.epoch)
                rows_before = evidence.rows_before
                epoch_rows = evidence.rows_after
                rows_loaded = evidence.rows_loaded
            else:
                epoch_rows = verify_epoch_loaded(spec.load_verify, target_dir, manifest.epoch)
                rows_loaded = max(epoch_rows - rows_before, 0)
            print(
                f"gate=load_verify status=pass epoch={manifest.epoch} "
                f"rows={epoch_rows} target={target_dir}"
            )
        except LoadVerifyError as exc:
            if not config.e2e_commissioning():
                raise
            load_verify_warning = f"{type(exc).__name__}: {exc}"
            print(
                "gate=load_verify status=commissioning_nonblocking "
                f"epoch={manifest.epoch} warning={load_verify_warning}"
            )

    return {
        "target_dir": target_dir,
        "epoch_rows": epoch_rows,
        "rows_before": rows_before,
        "rows_loaded": rows_loaded,
        "staging_verify": staging_verify,
        "load_verify_complete": bool(spec.load_verify),
        "load_verify_warning": load_verify_warning,
    }


def _run_post_gates_with_policy(**kwargs):
    """Run numeric gates, preserving observed failures only in commissioning mode."""
    try:
        return run_post_gates(**kwargs), None
    except (PostGateError, SigmaGateError) as exc:
        if not config.e2e_commissioning():
            raise
        warning = f"{type(exc).__name__}: {exc}"
        print(f"gate=post status=commissioning_nonblocking warning={warning}")
        return None, warning


def _isolated_load_target(
    *,
    activation_kind: ActivationKind,
    run_id: str,
    source_activation_enabled: bool,
    nsa_build_db: str | None,
    keyword_candidate_base: str | None,
) -> str | None:
    """Select the isolated database used by the category table adapter."""
    if activation_kind in {ActivationKind.CSD_CHANNEL, ActivationKind.CSD_KEYWORD}:
        if not source_activation_enabled:
            raise RuntimeError(
                f"{activation_kind.value} activation is not enabled; refusing table load"
            )
    match activation_kind:
        case ActivationKind.IQVIA_NSA:
            return nsa_build_db
        case ActivationKind.CSD_CHANNEL:
            return f"jw_ingest_csd_channel_{run_id}"
        case ActivationKind.CSD_KEYWORD:
            return keyword_candidate_base
        case ActivationKind.NONE | ActivationKind.UBIST_NUMERIC:
            return None


def _load_with_source_inventory(
    manifest,
    spec,
    input_root: Path,
    *,
    run_id: str,
    target_dir_override: Path | None,
    target_db_override: str | None = None,
    required: bool,
    rebuild_all_current: bool = False,
) -> tuple[dict, ScanOutcome | None]:
    """Run a source-wide scan and publish its immutable anchor after load."""
    policy = load_scan_policy(manifest.category, required=required)
    if policy is None:
        return (
            _real_load(
                manifest,
                spec,
                input_root,
                target_dir_override=target_dir_override,
                target_db_override=target_db_override,
            ),
            None,
        )
    previous = latest_successful_snapshot(DEFAULT_INVENTORY_ROOT, manifest.category)
    outcome = run_full_scan(
        policy,
        epoch=manifest.epoch,
        manifest_sha=manifest.manifest_sha,
        run_id=run_id,
        output_root=DEFAULT_INVENTORY_ROOT,
        previous=previous,
        bootstrap_files=tuple(
            (input_root / entry.path).resolve() for entry in manifest.files
        ),
        # Run-scoped databases start empty. Reuse classification metadata, but
        # feed every current source file to the loader that populates the new DB.
        rebuild_all_current=(target_db_override is not None or rebuild_all_current),
        permissive=config.e2e_commissioning(),
        rebuild=lambda source_files: _real_load(
            manifest,
            spec,
            input_root,
            target_dir_override=target_dir_override,
            source_files=source_files,
            target_db_override=target_db_override,
        ),
    )
    return dict(outcome.rebuild_result), outcome


def _automatic_publish_contract(outcome: ScanOutcome) -> dict[str, object]:
    """Serialize the approved hard-gate and warning contract for the hook."""
    gates = outcome.gates
    permissive = bool(outcome.commissioning_warnings)
    return {
        "hard_gates": {
            "PG-1": "pass",
            "PG-2": "pass",
            "PG-3": "pass",
            "PG-4": "pass" if permissive else gates.pg4.status,
            "PG-5": "pass" if permissive else gates.pg5.status,
        },
        "warnings": {
            "PG-6": gates.pg6.status,
            "PG-7": gates.pg7.status,
        },
        "inventory_snapshot": str(outcome.snapshot_path),
        "observed_hard_gates": {
            "PG-4": gates.pg4.status,
            "PG-5": gates.pg5.status,
        },
        "commissioning_warnings": list(outcome.commissioning_warnings),
    }


def _request_automatic_publish(
    identity: tuple[str, str, str],
    *,
    run_id: str,
    endpoint: str,
    opener=None,
) -> int:
    """Request exact automatic publication after the candidate is durable."""
    import json
    import urllib.request

    if not endpoint:
        raise RuntimeError("automatic publish endpoint is not configured")
    epoch, category, manifest_sha = identity
    body = json.dumps(
        {
            "epoch": epoch,
            "category": category,
            "manifest_sha": manifest_sha,
            "run_id": run_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    open_request = opener or urllib.request.urlopen
    with open_request(request, timeout=15) as response:
        status = int(getattr(response, "status", 0))
    if not 200 <= status < 300:
        raise RuntimeError(f"automatic publish request returned HTTP {status}")
    return status


def _emit_completion_signal(
    *, ledger: Ledger, tracker: _StageTracker, identity: tuple[str, str, str],
    run_id: str, event: str, mode: str, rows_before: int, rows_after: int,
    rows_loaded: int,
    periods: set[str], started_at: str, failure_reason: str | None,
    target_schema: str | None = None,
    published_at: str | None = None,
    affected_scope: dict[str, object] | None = None,
) -> None:
    """Best-effort delivery and durable observation; never changes ingest result."""
    from urllib.parse import urlencode

    from pipeline.scripts.ingest_hook.completion_signal import CompletionSignal, PublishResult, publish

    epoch, category, manifest_sha = identity
    # A retry of the same event may observe already-materialized staging rows and
    # otherwise emit zero. Freeze counts for that event only: an earlier failure
    # before load must not erase a later successful run's actual count.
    try:
        prior_signals = ledger.signal_events(*identity)
    except Exception:  # signal observation is best-effort
        prior_signals = []
    prior_for_event = next((signal for signal in prior_signals if signal.event == event), None)
    if prior_for_event is not None:
        try:
            prior_payload = prior_for_event.payload
            rows_before = int(prior_payload["rows_before"])
            rows_after = int(prior_payload["rows_after"])
            rows_loaded = int(prior_payload["rows_loaded"])
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[signal] prior payload invalid (ignored): {type(exc).__name__}: {exc}", file=sys.stderr)
    query = urlencode({"epoch": epoch, "category": category, "manifest_sha": manifest_sha})
    occurred_at = _stamp()
    try:
        signal = CompletionSignal(
            event=event, mode=mode, source=category, epoch=epoch,
            manifest_sha=manifest_sha, run_id=run_id,
            target_schema=(
                target_schema
                or os.environ.get("MARIADB_DATABASE")
                or os.environ.get("DB_NAME")
            ),
            published_at=published_at,
            occurred_at=occurred_at,
            rows_before=rows_before, rows_after=rows_after,
            rows_loaded=rows_loaded, period_from=min(periods) if periods else None,
            period_to=max(periods) if periods else None, started_at=started_at,
            finished_at=occurred_at, failure_reason=failure_reason,
            log_ref=f"/ingest/status?{query}",
            affected_scope=affected_scope,
        )
    except ValueError as exc:
        reason = f"completion contract rejected: {exc}"
        ledger.record_signal(
            *identity, run_id=run_id, event=event, mode=mode,
            rows_loaded=rows_loaded, delivery_status="failed", attempts=0,
            reason=reason,
            payload={
                "event": event,
                "run_id": run_id,
                "source": category,
                "period": epoch,
                "outbound": False,
            },
        )
        tracker.record_failure("signal", reason)
        print(f"[signal] {reason}; outbound delivery suppressed", file=sys.stderr)
        return
    payload = signal.as_dict()
    try:
        ledger.record_signal(
            *identity, run_id=run_id, event=event, mode=mode,
            rows_loaded=rows_loaded, delivery_status="pending",
            attempts=0, reason=None, payload=payload,
        )
    except Exception as exc:
        reason = f"ledger pending record failed: {type(exc).__name__}: {exc}"
        tracker.record_failure("signal", reason)
        print(f"[signal] {reason}; outbound delivery suppressed", file=sys.stderr)
        return
    try:
        endpoint, attempts = config.completion_webhook()
        result = publish(signal, endpoint=endpoint, attempts=attempts)
    except Exception as exc:  # malformed delivery config is also non-fatal
        result = PublishResult("failed", 0, f"{type(exc).__name__}: {exc}")
    try:
        ledger.record_signal(
            *identity, run_id=run_id, event=event, mode=mode,
            rows_loaded=rows_loaded, delivery_status=result.status,
            attempts=result.attempts, reason=result.reason, payload=payload,
        )
    except Exception as exc:
        result = PublishResult(
            "failed",
            result.attempts,
            f"ledger terminal record failed: {type(exc).__name__}: {exc}",
        )
        print(f"[signal] {result.reason}", file=sys.stderr)
    drain_result = PublishResult("disabled", 0, "queue drain endpoint is not configured")
    try:
        drain_endpoint, drain_attempts = config.queue_drain_webhook()
        if drain_endpoint:
            drain_result = publish(
                signal,
                endpoint=drain_endpoint,
                attempts=drain_attempts,
            )
    except Exception as exc:  # queue drain callback is recoverable by reconciliation
        drain_result = PublishResult("failed", 0, f"{type(exc).__name__}: {exc}")
    stage_reason = (
        f"delivery={result.status}; attempts={result.attempts}; "
        f"queue_drain={drain_result.status}; "
        f"queue_drain_attempts={drain_result.attempts}"
    )
    if result.status in {"published", "disabled"}:
        tracker.complete("signal", reason=stage_reason)
    else:
        tracker.record_failure("signal", stage_reason)
    print(
        f"signal event={event} mode={mode} delivery={result.status} "
        f"attempts={result.attempts} queue_drain={drain_result.status} "
        f"queue_drain_attempts={drain_result.attempts}"
    )


def _completion_affected_scope(category: str) -> dict[str, object] | None:
    if category == "iqvia_nsa":
        return {
            "dimension": "source",
            "count": 1,
            "values": [category],
        }
    return None


def run(
    manifest_path: Path,
    *,
    input_root: Path,
    ledger: Ledger,
    rehearsal_root: Path | None,
    run_id: str | None = None,
) -> int:
    run_id = run_id or _run_id()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        manifest = load_manifest(manifest_path)
    except ContractError as exc:
        print(f"gate=contract status=fail reason={exc}", file=sys.stderr)
        return 2

    identity = (manifest.epoch, manifest.category, manifest.manifest_sha)
    ledger = _ledger_for_run(ledger, identity, run_id)
    entry = ledger.status(*identity)
    if entry is None:
        # Standalone/sweep execution: register the identity before running.
        ledger.receive(*identity, manifest_path=str(manifest_path), uploaded_by=manifest.uploaded_by)
        entry = ledger.status(*identity)
    if entry.status == STATUS_COMPLETE:
        # Defence in depth: a re-delivered Job for a completed identity is a no-op.
        print(f"result=noop reason=identity already complete epoch={manifest.epoch} category={manifest.category}")
        return 0
    if entry.status in {STATUS_AWAITING_APPROVAL, STATUS_PUBLISH_RUNNING}:
        print(
            "result=noop "
            f"reason=identity already {entry.status} "
            f"epoch={manifest.epoch} category={manifest.category}"
        )
        return 0
    if entry.status == STATUS_QUEUED:
        claimed = ledger.mark_running(
            *identity,
            job_name=os.environ.get("HOSTNAME", f"local-{run_id}"),
            run_id=run_id,
        )
        if not claimed:
            print(
                "result=noop reason=queued identity was claimed concurrently "
                f"epoch={manifest.epoch} category={manifest.category}"
            )
            return 0

    tracker = _StageTracker(ledger, identity, run_id)
    mode = "staging" if rehearsal_root is not None else str(config.load_mode())
    rows_before = 0
    rows_after = 0
    rows_loaded = 0
    periods: set[str] = set()
    atc4_scope: tuple[str, ...] = ()
    writer_conn = None
    mart_conn = None
    corpus_candidate = None
    mart_activation = None
    nsa_activation = None
    keyword_activation = None
    writer_lock_acquired = False
    writer_lock_name = None
    publish_actions: tuple[object, ...] = ()
    activation_succeeded = False
    awaiting_approval_prepared = False
    ledger_completed = False
    completion_signal_emitted = False
    baseline_live_snapshot = None
    baseline_manifest_sha = None
    scan_outcome = None
    post_gate_verified = False
    retained_quarters: tuple[str, ...] = ()
    activation_journal = None
    primary_failure_reason = None
    try:
        spec = resolve_category(manifest.category)
        previous_total = ledger.previous_complete_total(manifest.category, before_epoch=manifest.epoch)

        # 1) G3 — always first; a failure here has zero DB effect.
        tracker.enter("g3")
        report = validate(manifest, spec, input_root, previous_total_rows=previous_total)
        periods = set(report.observed_periods)
        print(f"gate=g3 status=pass files={len(report.file_rows)} rows={report.total_rows}")
        tracker.done()

        # 2) load + 3) fail-closed post-gates
        if rehearsal_root is not None:
            tracker.enter("load")
            table = _rehearsal_load(manifest, input_root, rehearsal_root)
            tracker.done()
            tracker.skip("load_verify", "rehearsal (sqlite staging)")
            tracker.skip("mart_build", "rehearsal (mart untouched)")
            tracker.skip("sigma", "rehearsal (mart untouched)")
            conn = sqlite3.connect(str(rehearsal_root / "staging.db"))
            try:
                stable = SourceSnapshot((TableFingerprint("external_mart", 0, "untouched"),))

                def rehearsal_sigma() -> SigmaEvidence:
                    sigma = check_staging(conn, table)
                    checked = len(sigma.periods)
                    return SigmaEvidence(checked, checked, str(sigma.periods))

                tracker.enter("post_gate")
                actual_rows = staging_row_count(conn, table)
                rows_after = actual_rows
                rows_loaded = max(rows_after - rows_before, 0)
                post = run_post_gates(
                    run_id=run_id,
                    epoch=manifest.epoch,
                    category=manifest.category,
                    sigma_check=rehearsal_sigma,
                    expected_rows=report.total_rows,
                    actual_rows=actual_rows,
                    untouched_before=stable,
                    untouched_after=stable,
                    report_path=rehearsal_root / "post_gate_report.json",
                )
            finally:
                conn.close()
            print(f"gate=post status={post.status} duration_ms={post.duration_ms}")
            tracker.done()
            tracker.skip("mart_publish", "rehearsal (mart untouched)")
            tracker.skip("refresh", "rehearsal (orchestrator untouched)")
            print("phase=refresh status=skipped reason=rehearsal (orchestrator untouched)")
        else:
            before_snapshot = None
            configured_mode = config.load_mode()
            _, configured_staging_verify = config.load_output_root()
            is_shadow = configured_mode == "shadow"
            target_root, _ = config.load_output_root()
            source_activation_enabled = config.source_activation_enabled(
                manifest.category, mode=configured_mode
            )
            if manifest.category == "ubist" and not configured_staging_verify:
                from pipeline.scripts.ingest_hook import ubist_mart_activation

                if is_shadow:
                    mart_activation = ubist_mart_activation.shadow_from_env(run_id=run_id)
                    shadow_live_root = target_root / "ubist"
                    if not shadow_live_root.exists():
                        seed_value = os.environ.get("INGEST_SHADOW_SEED_ROOT", "").strip()
                        if not seed_value:
                            raise RuntimeError(
                                "shadow corpus is absent and INGEST_SHADOW_SEED_ROOT is not set"
                            )
                        ubist_mart_activation.ensure_shadow_corpus(
                            shadow_live_root, seed_root=Path(seed_value)
                        )
                else:
                    mart_activation = ubist_mart_activation.from_env(run_id=run_id)
            elif manifest.category == "iqvia_nsa" and not configured_staging_verify:
                from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation

                iqvia_nsa_mart_activation.require_production_mode(mode)
                nsa_activation = iqvia_nsa_mart_activation.from_env(run_id=run_id)
                iqvia_nsa_mart_activation.initialize_build_schema(nsa_activation)
            elif (
                spec.activation_kind is ActivationKind.CSD_KEYWORD
                and not configured_staging_verify
                and config.source_activation_enabled(manifest.category, mode=configured_mode)
            ):
                from pipeline.scripts.ingest_hook import csd_keyword_activation

                keyword_raw_schema, keyword_stage_schema = (
                    config.csd_keyword_live_schemas()
                )
                keyword_activation = csd_keyword_activation.plan_for_run(
                    run_id,
                    raw_schema=keyword_raw_schema,
                    stage_schema=keyword_stage_schema,
                )
            if spec.sigma_source and not configured_staging_verify:
                source_db = (
                    mart_activation.source_db
                    if mart_activation is not None
                    else nsa_activation.source_db
                    if nsa_activation is not None
                    else None
                )
                baseline_conn = config.open_mart_connection(source_db)
                baseline_lock_acquired = False
                baseline_failure_reason = None
                try:
                    if manifest.category == "ubist" and is_shadow:
                        recovery_lock_name = ubist_mart_activation.shadow_lock_name(
                            mart_activation.target_db
                        )
                        recovery_lock_acquired = False
                        recovery_failure_reason = None
                        try:
                            ubist_mart_activation.acquire_writer_lock(
                                baseline_conn,
                                timeout_seconds=0,
                                lock_name=recovery_lock_name,
                            )
                            recovery_lock_acquired = True
                            ubist_mart_activation.ensure_shadow_target_baseline(
                                baseline_conn, mart_activation
                            )
                            recovered = ubist_mart_activation.recover_incomplete_activations(
                                baseline_conn,
                                output_root=target_root,
                                required_target_prefix=ubist_mart_activation.SHADOW_DB_PREFIX,
                                ledger_status=lambda epoch, category, manifest_sha: (
                                    entry.status
                                    if (
                                        entry := ledger.status(epoch, category, manifest_sha)
                                    )
                                    is not None
                                    else None
                                ),
                            )
                            if recovered:
                                ubist_mart_activation.validate_shadow_publish(
                                    baseline_conn, mart_activation
                                )
                                ubist_mart_activation.complete_recovery(recovered)
                        except Exception as exc:
                            recovery_failure_reason = f"{type(exc).__name__}: {exc}"
                            raise
                        finally:
                            if recovery_lock_acquired:
                                _release_writer_lock_preserving_primary(
                                    baseline_conn,
                                    lock_name=recovery_lock_name,
                                    primary_failure_reason=recovery_failure_reason,
                                )
                    elif manifest.category == "ubist":
                        from pipeline.scripts.ingest_hook import ubist_mart_activation

                        ubist_mart_activation.acquire_writer_lock(baseline_conn, timeout_seconds=0)
                        baseline_lock_acquired = True
                        recovered = ubist_mart_activation.recover_incomplete_activations(
                            baseline_conn,
                            output_root=target_root,
                            ledger_status=lambda epoch, category, manifest_sha: (
                                entry.status
                                if (entry := ledger.status(epoch, category, manifest_sha)) is not None
                                else None
                            ),
                        )
                        if recovered:
                            _run_recovery_refresh(
                                tracker=_recovery_tracker(
                                    ledger,
                                    identity,
                                    run_id=run_id,
                                    phase="startup",
                                ),
                                argv=spec.refresh_argv,
                                connection=baseline_conn,
                                lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
                            )
                            ubist_mart_activation.complete_recovery(recovered)
                    before_snapshot = fingerprint_untouched_sources(
                        baseline_conn, touched_source=spec.sigma_source
                    )
                    baseline_live_snapshot = fingerprint_untouched_sources(
                        baseline_conn, touched_source="__jw_ingest_no_source__"
                    )
                    if manifest.category == "ubist":
                        baseline_manifest_sha = ubist_mart_activation.corpus_manifest_sha(
                            target_root / "ubist"
                        )
                except Exception as exc:
                    baseline_failure_reason = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    if baseline_lock_acquired:
                        _release_writer_lock_preserving_primary(
                            baseline_conn,
                            lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
                            primary_failure_reason=baseline_failure_reason,
                        )
                    baseline_conn.close()
            if manifest.category == "ubist" and not configured_staging_verify:
                corpus_candidate = ubist_mart_activation.prepare_candidate_corpus(
                    target_root / "ubist", run_id=run_id
                )
            # 2) real load — wire the materialized upload in, prove the epoch landed (M-2).
            tracker.enter("load")
            load_result, scan_outcome = _load_with_source_inventory(
                manifest,
                spec,
                input_root,
                run_id=run_id,
                target_dir_override=(corpus_candidate.candidate_root if corpus_candidate else None),
                required=config.full_scan_enabled(),
                target_db_override=_isolated_load_target(
                    activation_kind=spec.activation_kind,
                    run_id=run_id,
                    source_activation_enabled=source_activation_enabled,
                    nsa_build_db=(
                        nsa_activation.build_db if nsa_activation is not None else None
                    ),
                    keyword_candidate_base=(
                        keyword_activation.candidate_base
                        if keyword_activation is not None
                        else None
                    ),
                ),
                rebuild_all_current=isinstance(ledger, _ReingestAttemptLedger),
            )
            rows_before = int(load_result.get("rows_before") or 0)
            rows_after = int(load_result.get("epoch_rows") or 0)
            rows_loaded = int(load_result.get("rows_loaded") or 0)
            tracker.done()
            if load_result.get("load_verify_complete"):
                tracker.complete("load_verify", load_result.get("load_verify_warning"))
            else:
                tracker.skip("load_verify", "category has no load_verify spec")
            staging_verify = load_result["staging_verify"]
            if (
                spec.activation_kind is ActivationKind.CSD_CHANNEL
                and source_activation_enabled
            ):
                from pipeline.scripts.ingest_hook import csd_channel_activation
                from pipeline.scripts.ingest_hook.ubist_mart_activation import (
                    acquire_writer_lock,
                )

                tracker.skip("mart_build", "CSD channel is not eligible for numeric mart build")
                tracker.skip("sigma", "CSD channel uses source-specific validation")
                commissioning = config.e2e_commissioning()
                if commissioning:
                    tracker.skip("post_gate", "commissioning no-op")
                else:
                    tracker.enter("post_gate")
                raw_schema, stage_schema = config.csd_channel_live_schemas(
                    mode=configured_mode
                )
                csd_plan = csd_channel_activation.plan_for_run(
                    run_id,
                    raw_schema=raw_schema,
                    stage_schema=stage_schema,
                )
                csd_conn = config.open_csd_channel_connection()
                csd_lock_acquired = False
                csd_failure_reason = None
                try:
                    # GET_LOCK is node-local in Galera. The durable single-writer
                    # reservation is the ledger queued->running CAS performed by
                    # this runner; the lock only protects this connection's node.
                    acquire_writer_lock(
                        csd_conn,
                        timeout_seconds=0,
                        lock_name=csd_channel_activation.WRITER_LOCK_NAME,
                    )
                    csd_lock_acquired = True
                    csd_evidence = csd_channel_activation.prepare_candidate(
                        csd_conn,
                        csd_plan,
                        source_paths=tuple(
                            (input_root / entry.path).resolve()
                            for entry in manifest.files
                        ),
                        enforce_post_gate=not commissioning,
                    )
                except Exception as exc:
                    csd_failure_reason = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    if csd_lock_acquired:
                        _release_writer_lock_preserving_primary(
                            csd_conn,
                            lock_name=csd_channel_activation.WRITER_LOCK_NAME,
                            primary_failure_reason=csd_failure_reason,
                        )
                    csd_conn.close()
                if not commissioning:
                    tracker.done()
                prepared_at = datetime.now(timezone.utc)
                expires_at = prepared_at + timedelta(
                    seconds=_publish_candidate_ttl_seconds()
                )
                candidate_payload = {
                    "epoch": manifest.epoch,
                    "category": manifest.category,
                    "manifest_sha": manifest.manifest_sha,
                    "run_id": run_id,
                    "mode": configured_mode,
                    "rows_before": rows_before,
                    "rows_after": csd_evidence.raw.row_count,
                    "rows_loaded": rows_loaded,
                    "csd_activation_plan": csd_channel_activation.plan_payload(csd_plan),
                    "csd_candidate_evidence": csd_channel_activation.evidence_payload(
                        csd_evidence
                    ),
                }
                if scan_outcome is not None:
                    candidate_payload["automatic_publish"] = _automatic_publish_contract(
                        scan_outcome
                    )
                ledger.mark_awaiting_approval(
                    *identity,
                    run_id=run_id,
                    candidate=candidate_payload,
                    prepared_at=prepared_at.isoformat(),
                    expires_at=expires_at.isoformat(),
                )
                tracker.skip("mart_publish", "awaiting explicit publish approval")
                tracker.skip(
                    "refresh",
                    "CSD channel API reads the activated stage table directly",
                )
                awaiting_approval_prepared = True
                ledger.record_signal(
                    *identity,
                    run_id=run_id,
                    event="prepared",
                    mode=configured_mode,
                    rows_loaded=rows_loaded,
                    delivery_status="suppressed",
                    attempts=0,
                    reason="prepared event is internal-only",
                    payload={"event": "prepared", "outbound": False},
                )
                completion_signal_emitted = True
                print(
                    "result=awaiting_approval "
                    f"epoch={manifest.epoch} category={manifest.category} run_id={run_id}"
                )
                if scan_outcome is not None:
                    status = _request_automatic_publish(
                        identity,
                        run_id=run_id,
                        endpoint=config.automatic_publish_webhook(),
                    )
                    print(f"result=automatic_publish_requested http_status={status}")
                return 0
            if keyword_activation is not None:
                from pipeline.scripts.ingest_hook import csd_keyword_activation

                tracker.skip("mart_build", "CSD keyword uses source-table activation")
                tracker.skip("sigma", "CSD keyword uses source-table evidence")
                tracker.enter("post_gate")
                keyword_conn = config.open_mart_connection()
                try:
                    keyword_evidence = csd_keyword_activation.validate_candidate(
                        keyword_conn, keyword_activation
                    )
                finally:
                    keyword_conn.close()
                tracker.done()
                prepared_at = datetime.now(timezone.utc)
                expires_at = prepared_at + timedelta(
                    seconds=_publish_candidate_ttl_seconds()
                )
                candidate_payload = {
                    "epoch": manifest.epoch,
                    "category": manifest.category,
                    "manifest_sha": manifest.manifest_sha,
                    "run_id": run_id,
                    "mode": configured_mode,
                    "rows_before": rows_before,
                    "rows_after": keyword_evidence.raw_rows,
                    "rows_loaded": rows_loaded,
                    "keyword_activation_plan": csd_keyword_activation.plan_payload(
                        keyword_activation
                    ),
                    "keyword_candidate_evidence": csd_keyword_activation.evidence_payload(
                        keyword_evidence
                    ),
                }
                if scan_outcome is not None:
                    candidate_payload["automatic_publish"] = _automatic_publish_contract(
                        scan_outcome
                    )
                ledger.mark_awaiting_approval(
                    *identity,
                    run_id=run_id,
                    candidate=candidate_payload,
                    prepared_at=prepared_at.isoformat(),
                    expires_at=expires_at.isoformat(),
                )
                tracker.skip("mart_publish", "automatic keyword publish requested")
                awaiting_approval_prepared = True
                ledger.record_signal(
                    *identity,
                    run_id=run_id,
                    event="prepared",
                    mode=configured_mode,
                    rows_loaded=rows_loaded,
                    delivery_status="suppressed",
                    attempts=0,
                    reason="prepared event is internal-only",
                    payload={"event": "prepared", "outbound": False},
                )
                tracker.skip("signal", "prepared event is not outbound")
                status = _request_automatic_publish(
                    identity,
                    run_id=run_id,
                    endpoint=config.automatic_publish_webhook(),
                )
                print(f"result=automatic_publish_requested http_status={status}")
                return 0
            if staging_verify:
                # Isolated J5 verification: real loader exercised, zero mart write.
                tracker.skip("mart_build", "staging-verify (mart untouched)")
                tracker.skip("sigma", "staging-verify (mart untouched)")
                tracker.skip("post_gate", "staging-verify (mart untouched)")
                tracker.skip("mart_publish", "staging-verify (mart untouched)")
                tracker.skip("refresh", "staging-verify (orchestrator untouched)")
                print("gate=sigma status=skipped reason=staging-verify (mart untouched)")
                print("phase=refresh status=skipped reason=staging-verify (orchestrator untouched)")
            else:
                # 3) Build an isolated mart from the candidate corpus, then gate it.
                if mart_activation is not None:
                    from pipeline.scripts.ingest_hook import ubist_mart_activation

                    tracker.enter("mart_build")
                    catalog_root = (
                        ubist_mart_activation.shadow_catalog_root_from_env(target_root)
                        if is_shadow
                        else ubist_mart_activation.production_catalog_root_from_env()
                    )
                    catalog_conn = config.open_mart_connection(mart_activation.source_db)
                    try:
                        catalog_preparation = ubist_mart_activation.prepare_catalog_for_mart(
                            catalog_root=catalog_root,
                            ubist_dir=target_root / "ubist",
                            source_db=mart_activation.source_db,
                            conn=catalog_conn,
                            run_id=run_id,
                            output_parent=target_root,
                        )
                    finally:
                        catalog_conn.close()
                    print(
                        "phase=catalog_preflight status=complete "
                        f"action={catalog_preparation.action} "
                        f"mi_master_sha256={catalog_preparation.mi_master_sha256} "
                        f"parity_tables={len(catalog_preparation.parity)}"
                    )
                    print(
                        f"phase=mart_build status=start build_db={mart_activation.build_db} "
                        f"catalog_root={catalog_root} ubist_dir={load_result['target_dir']}"
                    )
                    loaded_periods = (manifest.epoch,)
                    atc4_scope = ubist_mart_activation.affected_atc4_codes(
                        load_result["target_dir"],
                        periods=loaded_periods,
                    )
                    print(
                        "phase=mart_build mode=incremental "
                        f"periods={loaded_periods} "
                        f"atc4_count={len(atc4_scope)} atc4_scope={atc4_scope}"
                    )
                    ubist_mart_activation.build_shadow(
                        mart_activation,
                        catalog_root=catalog_root,
                        ubist_dir=load_result["target_dir"],
                        atc4_scope=atc4_scope,
                        period_scope=loaded_periods,
                    )
                    mart_conn = config.open_mart_connection(mart_activation.build_db)
                    print(f"phase=mart_build status=complete build_db={mart_activation.build_db}")
                    tracker.done()
                elif nsa_activation is not None:
                    from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation

                    tracker.enter("mart_build")
                    raw_conn = config.open_mart_connection(nsa_activation.build_db)
                    try:
                        retained_quarters = iqvia_nsa_mart_activation.trim_raw_retention(
                            raw_conn, nsa_activation
                        )
                    finally:
                        raw_conn.close()
                    print(
                        f"phase=mart_build status=start build_db={nsa_activation.build_db} "
                        f"retained_quarters={retained_quarters}"
                    )
                    iqvia_nsa_mart_activation.build_mart(nsa_activation)
                    mart_conn = config.open_mart_connection(nsa_activation.build_db)
                    print(
                        f"phase=mart_build status=complete build_db={nsa_activation.build_db}"
                    )
                    tracker.done()
                elif spec.sigma_source:
                    raise RuntimeError("live mart load has no isolated activation plan")
                else:
                    tracker.skip("mart_build", "category has no mart activation")

                # 4) Σ/post-gate checks run against the isolated build, never stale live tables.
                if spec.sigma_source:
                    from pipeline.scripts.ingest_hook.sigma_market import check_market_sigma

                    affected = tuple(sorted(report.observed_periods)) or (manifest.epoch,)
                    sampled = sample_existing_periods(
                        mart_conn, source=spec.sigma_source, excluded=affected
                    )
                    periods = tuple(sorted(set(affected + sampled)))
                    if is_shadow:
                        injected = ubist_mart_activation.maybe_inject_shadow_sigma_mismatch(
                            mart_conn,
                            source=spec.sigma_source,
                            periods=periods,
                        )
                        if injected:
                            print(f"shadow_failure=sigma_parts_whole evidence={injected}")

                    def mart_sigma() -> SigmaEvidence:
                        sigma = check_market_sigma(
                            mart_conn, source=spec.sigma_source, periods=periods
                        )
                        return SigmaEvidence(
                            sigma.cells_checked,
                            sigma.cells_checked,
                            f"markets={sigma.markets_checked} periods={periods} "
                            f"worst_rel={sigma.worst_rel:.6%}",
                        )

                    if mart_conn is None or before_snapshot is None:
                        raise RuntimeError("live post-gate requires a mart connection and baseline")
                    tracker.enter("post_gate")
                    post_gate_actual_rows = int(load_result["epoch_rows"] or 0)
                    if is_shadow:
                        post_gate_actual_rows = (
                            ubist_mart_activation.shadow_post_gate_actual_rows(
                                post_gate_actual_rows
                            )
                        )
                    post, post_gate_warning = _run_post_gates_with_policy(
                        run_id=run_id,
                        epoch=manifest.epoch,
                        category=manifest.category,
                        sigma_check=mart_sigma,
                        expected_rows=report.total_rows,
                        actual_rows=post_gate_actual_rows,
                        untouched_before=before_snapshot,
                        untouched_after=fingerprint_untouched_sources(
                            mart_conn, touched_source=spec.sigma_source
                        ),
                        report_path=load_result["target_dir"] / "post_gate_report.json",
                    )
                    if post is not None:
                        print(f"gate=post status={post.status} duration_ms={post.duration_ms}")
                    post_gate_verified = True
                    tracker.done(reason=post_gate_warning)
                    tracker.complete("sigma", post_gate_warning)
                else:
                    tracker.skip("sigma", "category has no sigma_source")
                    tracker.skip("post_gate", "category has no sigma_source")

                # 5) Only a gated candidate may replace corpus + serving mart.
                if mart_activation is not None:
                    from pipeline.scripts.ingest_hook import ubist_mart_activation

                    activation_journal = ubist_mart_activation.write_activation_journal(
                        corpus_candidate,
                        mart_activation,
                        run_id=run_id,
                        phase="awaiting_approval",
                        identity=identity,
                    )
                    if load_result["epoch_rows"] is not None:
                        report.file_rows[f"epoch:{manifest.epoch}"] = load_result["epoch_rows"]
                    prepared_at = datetime.now(timezone.utc)
                    expires_at = prepared_at + timedelta(
                        seconds=_publish_candidate_ttl_seconds()
                    )
                    candidate_integrity = ubist_mart_activation.inventory_corpus(
                        corpus_candidate.candidate_root
                    )
                    build_table_integrity = ubist_mart_activation.fingerprint_build_tables(
                        mart_conn, mart_activation.build_db
                    )
                    candidate_payload = {
                        "epoch": manifest.epoch,
                        "category": manifest.category,
                        "manifest_sha": manifest.manifest_sha,
                        "run_id": run_id,
                        "mode": mode,
                        "activation_journal": str(activation_journal),
                        "live_root": str(corpus_candidate.live_root),
                        "candidate_root": str(corpus_candidate.candidate_root),
                        "backup_root": str(corpus_candidate.backup_root),
                        "source_db": mart_activation.source_db,
                        "target_db": mart_activation.target_db,
                        "build_db": mart_activation.build_db,
                        "baseline_manifest_sha": baseline_manifest_sha,
                        "baseline_live_snapshot": [
                            {
                                "table": item.table,
                                "row_count": item.row_count,
                                "sample_sha256": item.sample_sha256,
                            }
                            for item in (
                                getattr(baseline_live_snapshot, "tables", ())
                                if baseline_live_snapshot is not None
                                else ()
                            )
                        ],
                        "rows_before": rows_before,
                        "rows_after": rows_after,
                        "rows_loaded": rows_loaded,
                        "row_counts": report.file_rows,
                        "periods": sorted(periods),
                        "affected_scope": {
                            "dimension": "atc4",
                            "count": len(atc4_scope),
                            "values": list(atc4_scope),
                        },
                        "candidate_integrity": {
                            "file_count": candidate_integrity.file_count,
                            "total_bytes": candidate_integrity.total_bytes,
                            "manifest_sha": candidate_integrity.manifest_sha,
                        },
                        "build_table_integrity": [
                            {
                                "table": item.table,
                                "row_count": item.row_count,
                                "crc_sum": item.crc_sum,
                                "crc_xor": item.crc_xor,
                            }
                            for item in build_table_integrity
                        ],
                    }
                    if scan_outcome is not None:
                        candidate_payload["automatic_publish"] = _automatic_publish_contract(
                            scan_outcome
                        )
                        candidate_payload["source_inventory"] = {
                            "snapshot_path": str(scan_outcome.snapshot_path),
                            "classified_count": scan_outcome.snapshot.classified_count,
                            "excluded_count": scan_outcome.snapshot.excluded_count,
                            "rejected_count": scan_outcome.snapshot.rejected_count,
                            "periods": list(scan_outcome.snapshot.periods),
                        }
                    ledger.mark_awaiting_approval(
                        *identity,
                        run_id=run_id,
                        candidate=candidate_payload,
                        prepared_at=prepared_at.isoformat(),
                        expires_at=expires_at.isoformat(),
                    )
                    tracker.skip("mart_publish", "awaiting explicit publish approval")
                    tracker.skip("refresh", "awaiting explicit publish approval")
                    awaiting_approval_prepared = True
                    ledger.record_signal(
                        *identity,
                        run_id=run_id,
                        event="prepared",
                        mode=mode,
                        rows_loaded=rows_loaded,
                        delivery_status="suppressed",
                        attempts=0,
                        reason="prepared event is internal-only",
                        payload={
                            "event": "prepared",
                            "run_id": run_id,
                            "source": manifest.category,
                            "period": manifest.epoch,
                            "outbound": False,
                        },
                    )
                    tracker.skip("signal", "prepared event is not outbound")
                    print(
                        "result=awaiting_approval "
                        f"epoch={manifest.epoch} category={manifest.category} run_id={run_id}"
                    )
                    if scan_outcome is not None:
                        try:
                            status = _request_automatic_publish(
                                identity,
                                run_id=run_id,
                                endpoint=config.automatic_publish_webhook(),
                            )
                            print(f"result=automatic_publish_requested http_status={status}")
                        except Exception as exc:
                            # The durable candidate remains recoverable by hook startup/reconcile.
                            print(
                                "result=automatic_publish_deferred "
                                f"reason={type(exc).__name__}: {exc}",
                                file=sys.stderr,
                            )
                    return 0
                elif nsa_activation is not None:
                    from pipeline.scripts.ingest_hook import (
                        iqvia_nsa_mart_activation,
                        ubist_mart_activation,
                    )
                    from pipeline.scripts.ingest_hook.iqvia_nsa_publication import (
                        build_publication_evidence,
                    )

                    tracker.enter("mart_publish")
                    writer_conn = config.open_mart_connection(nsa_activation.target_db)
                    writer_lock_name = ubist_mart_activation.WRITER_LOCK_NAME
                    ubist_mart_activation.acquire_writer_lock(
                        writer_conn, timeout_seconds=0, lock_name=writer_lock_name
                    )
                    writer_lock_acquired = True
                    ubist_mart_activation.require_writer_lock_owner(
                        writer_conn, lock_name=writer_lock_name
                    )
                    snapshot_conn = config.open_mart_connection(nsa_activation.source_db)
                    try:
                        current_live_snapshot = fingerprint_untouched_sources(
                            snapshot_conn, touched_source="__jw_ingest_no_source__"
                        )
                    finally:
                        snapshot_conn.close()
                    if current_live_snapshot != baseline_live_snapshot:
                        raise RuntimeError(
                            "serving general mart changed while NSA candidate was built"
                        )
                    publish_actions = iqvia_nsa_mart_activation.publish(
                        writer_conn,
                        nsa_activation,
                        run_id=run_id,
                        epoch=manifest.epoch,
                        post_gate_verified=post_gate_verified,
                        publication_evidence=build_publication_evidence(
                            manifest.files,
                            report.file_rows,
                            retained_quarters,
                        ),
                    )
                    print(
                        f"phase=mart_publish status=complete tables={len(publish_actions)}"
                    )
                    tracker.done()
                else:
                    tracker.skip("mart_publish", "category has no mart activation")
                tracker.enter("refresh")
                if activation_journal is not None:
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "refresh_started"
                    )
                if is_shadow and mart_activation is not None:
                    counts = ubist_mart_activation.validate_shadow_publish(
                        writer_conn, mart_activation
                    )
                    print(
                        "phase=refresh mode=shadow status=complete "
                        f"target_db={mart_activation.target_db} counts={counts}"
                    )
                else:
                    if writer_lock_acquired and writer_conn is not None:
                        _run_commands_with_writer_lock(
                            "refresh",
                            spec.refresh_argv,
                            connection=writer_conn,
                            lock_name=writer_lock_name,
                        )
                    else:
                        _run_commands("refresh", spec.refresh_argv)
                if activation_journal is not None:
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "refresh_succeeded"
                    )
                tracker.done()
                if load_result["epoch_rows"] is not None:
                    report.file_rows[f"epoch:{manifest.epoch}"] = load_result["epoch_rows"]
                if activation_journal is not None:
                    ledger.mark_complete(*identity, row_counts=report.file_rows)
                    ledger_completed = True
                    activation_succeeded = True
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "ledger_complete"
                    )
                    _emit_completion_signal(
                        ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
                        event="complete", mode=mode, rows_before=rows_before,
                        rows_after=rows_after, rows_loaded=rows_loaded,
                        periods=periods, started_at=started_at, failure_reason=None,
                        affected_scope=_completion_affected_scope(manifest.category),
                    )
                    completion_signal_emitted = True
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "signal_complete"
                    )
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "complete"
                    )
            if (
                activation_journal is None
                and load_result["epoch_rows"] is not None
            ):
                report.file_rows[f"epoch:{manifest.epoch}"] = load_result["epoch_rows"]

        if not ledger_completed:
            ledger.mark_complete(*identity, row_counts=report.file_rows)
            ledger_completed = True
        if not completion_signal_emitted:
            _emit_completion_signal(
                ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
                event="complete", mode=mode, rows_before=rows_before, rows_after=rows_after,
                rows_loaded=rows_loaded,
                periods=periods, started_at=started_at, failure_reason=None,
                affected_scope=_completion_affected_scope(manifest.category),
            )
        print(f"result=complete epoch={manifest.epoch} category={manifest.category} run_id={run_id}")
        return 0
    except PostGateError as exc:
        primary_failure_reason = f"{type(exc).__name__}: {exc}"
        tracker.fail(f"{type(exc).__name__}: {exc}")
        ledger.mark_gate_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        _emit_completion_signal(
            ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
            event="gate_failed", mode=mode, rows_before=rows_before, rows_after=rows_after,
            rows_loaded=rows_loaded,
            periods=periods, started_at=started_at,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        print(f"result=gate_failed reason={exc}", file=sys.stderr)
        return 1
    except (G3Error, SigmaGateError) as exc:
        primary_failure_reason = f"{type(exc).__name__}: {exc}"
        tracker.fail(f"{type(exc).__name__}: {exc}")
        ledger.mark_gate_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        _emit_completion_signal(
            ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
            event="gate_failed", mode=mode, rows_before=rows_before, rows_after=rows_after,
            rows_loaded=rows_loaded,
            periods=periods, started_at=started_at,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        print(f"result=gate_failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except (UnknownCategoryError, RuntimeError) as exc:
        primary_failure_reason = f"{type(exc).__name__}: {exc}"
        if ledger_completed:
            print(
                f"result=committed_with_postcommit_error reason={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        tracker.fail(f"{type(exc).__name__}: {exc}")
        ledger.mark_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        _emit_completion_signal(
            ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
            event="failed", mode=mode, rows_before=rows_before, rows_after=rows_after,
            rows_loaded=rows_loaded,
            periods=periods, started_at=started_at,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        print(f"result=failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail loud while preserving ledger/signal evidence
        primary_failure_reason = f"{type(exc).__name__}: {exc}"
        if ledger_completed:
            print(
                f"result=committed_with_postcommit_error reason={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        tracker.fail(f"{type(exc).__name__}: {exc}")
        ledger.mark_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        _emit_completion_signal(
            ledger=ledger, tracker=tracker, identity=identity, run_id=run_id,
            event="failed", mode=mode, rows_before=rows_before, rows_after=rows_after,
            rows_loaded=rows_loaded,
            periods=periods, started_at=started_at,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        print(f"result=failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if mart_activation is not None and not activation_succeeded and not awaiting_approval_prepared:
            from pipeline.scripts.ingest_hook import ubist_mart_activation

            if activation_journal is not None and writer_conn is not None:
                recovered = ubist_mart_activation.recover_incomplete_activations(
                    writer_conn,
                    output_root=activation_journal.parent,
                )
                if recovered:
                    try:
                        if mode == "shadow":
                            ubist_mart_activation.validate_shadow_publish(
                                writer_conn, mart_activation
                            )
                        else:
                            _run_recovery_refresh(
                                tracker=_recovery_tracker(
                                    ledger,
                                    identity,
                                    run_id=run_id,
                                    phase="failure",
                                ),
                                argv=resolve_category("ubist").refresh_argv,
                                connection=writer_conn,
                                lock_name=writer_lock_name,
                            )
                    except Exception as recovery_exc:
                        print(
                            f"recovery=deferred reason={type(recovery_exc).__name__}: {recovery_exc}",
                            file=sys.stderr,
                        )
                    else:
                        ubist_mart_activation.complete_recovery(recovered)
            elif corpus_candidate is not None:
                ubist_mart_activation.rollback_candidate_corpus(corpus_candidate)
        if (
            nsa_activation is not None
            and publish_actions
            and not ledger_completed
            and writer_conn is not None
        ):
            from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation

            try:
                iqvia_nsa_mart_activation.rollback_publication(
                    writer_conn,
                    nsa_activation,
                    actions=publish_actions,
                    run_id=run_id,
                    restore_run_id=f"failed_{run_id}",
                )
                _run_commands("refresh-restored-serving", resolve_category("iqvia_nsa").refresh_argv)
            except Exception as recovery_exc:
                print(
                    "recovery=failed "
                    f"reason={type(recovery_exc).__name__}: {recovery_exc}",
                    file=sys.stderr,
                )
        if writer_lock_acquired and writer_conn is not None:
            _release_writer_lock_preserving_primary(
                writer_conn,
                lock_name=writer_lock_name,
                primary_failure_reason=primary_failure_reason,
            )
        if mart_conn is not None and mart_conn is not writer_conn:
            mart_conn.close()
        if writer_conn is not None:
            writer_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.scripts.ingest_hook.job_runner")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--input-root", type=Path, default=None)
    parser.add_argument("--rehearsal-root", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    rehearsal_env = os.environ.get(config.ENV_REHEARSAL_ROOT, "")
    rehearsal_root = args.rehearsal_root or (Path(rehearsal_env) if rehearsal_env else None)
    ledger = config.open_configured_ledger()

    s3 = config.open_input_source()
    if s3 is not None:
        import tempfile

        from pipeline.scripts.ingest_hook.contract import parse_manifest_bytes

        workdir = Path(tempfile.mkdtemp(prefix="ingest_s3_"))
        manifest_key = str(args.manifest).lstrip("/")
        try:
            manifest_bytes = s3.read(manifest_key)
        except FileNotFoundError:
            print(f"gate=contract status=fail reason=manifest not found in bucket: {manifest_key}", file=sys.stderr)
            return 2
        local_manifest = workdir / manifest_key
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_bytes(manifest_bytes)
        try:
            manifest = parse_manifest_bytes(manifest_bytes, manifest_path=manifest_key)
            for entry in manifest.files:
                try:
                    s3.materialize([entry.path], workdir)
                except FileNotFoundError:
                    pass  # G3 reports the absence as a failure
        except Exception:
            pass  # contract failures surface in run()
        return run(
            local_manifest,
            input_root=workdir,
            ledger=ledger,
            rehearsal_root=rehearsal_root,
            run_id=args.run_id,
        )

    input_root = args.input_root or config.input_root()
    return run(
        args.manifest,
        input_root=input_root,
        ledger=ledger,
        rehearsal_root=rehearsal_root,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
