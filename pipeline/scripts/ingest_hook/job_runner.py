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
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.category_map import UnknownCategoryError, resolve_category
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, STATUS_QUEUED, Ledger
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

    def done(self, rc: int = 0) -> None:
        if self._current is None:
            return
        name, dur = self._current, self._elapsed_ms()
        self._record(name, "complete", finished=_stamp(), duration_ms=dur)
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


_EMPTY_UBIST_MANIFEST = '{"schema_version": "1.0", "partitions": []}'


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
    run_id: str | None = None,
) -> dict:
    """Wire the materialized upload into the loader, run it, and prove the epoch
    landed (M-2). Returns {target_dir, epoch_rows, staging_verify}.

    Fail-closed rules:
      * a category with a load_argv but no load_input_flag is UNWIRED — refuse
        to run it in real mode (it would load unrelated defaults = silent failure).
      * the epoch must appear in the loader's own output with rows > 0.
    """
    from pipeline.scripts.ingest_hook.load_verify import verify_epoch_loaded, verify_table_load

    if not spec.load_argv:
        return {"target_dir": None, "epoch_rows": None, "staging_verify": None}  # e.g. skeleton

    if not spec.load_input_flag:
        raise RuntimeError(
            f"category {manifest.category!r} has a load command but no upload wiring "
            "(load_input_flag); refusing to load unrelated defaults (silent-failure guard)"
        )

    target_root, staging_verify = config.load_output_root()
    if not staging_verify and not spec.production_load_supported:
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

    read_files = [str((input_root / entry.path).resolve()) for entry in manifest.files]
    source_batches = [read_files] if spec.load_batch_files else [[source] for source in read_files]
    for sources in source_batches:
        argv = list(spec.load_argv)
        shadow_overlap_dedup = (
            manifest.category == "ubist" and config.load_mode() == "shadow"
        )
        if shadow_overlap_dedup:
            argv.append("--allow-overlap-dedup")
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
        previous_run_id = os.environ.get("INGEST_RUN_ID")
        if run_id is not None:
            os.environ["INGEST_RUN_ID"] = run_id
        try:
            _run_commands("load", tuple(argv))
        finally:
            if run_id is not None:
                if previous_run_id is None:
                    os.environ.pop("INGEST_RUN_ID", None)
                else:
                    os.environ["INGEST_RUN_ID"] = previous_run_id

    # M-2: the uploaded epoch must be present in the loader's output.
    epoch_rows = None
    rows_loaded = 0
    if spec.load_verify:
        if spec.load_verify == "table_manifest":
            evidence = verify_table_load(target_dir, manifest.epoch)
            rows_before = evidence.rows_before
            epoch_rows = evidence.rows_after
            rows_loaded = evidence.rows_loaded
        else:
            epoch_rows = verify_epoch_loaded(spec.load_verify, target_dir, manifest.epoch)
            rows_loaded = max(epoch_rows - rows_before, 0)
        print(f"gate=load_verify status=pass epoch={manifest.epoch} rows={epoch_rows} target={target_dir}")

    return {
        "target_dir": target_dir,
        "epoch_rows": epoch_rows,
        "rows_before": rows_before,
        "rows_loaded": rows_loaded,
        "staging_verify": staging_verify,
    }


def _emit_completion_signal(
    *, ledger: Ledger, tracker: _StageTracker, identity: tuple[str, str, str],
    run_id: str, event: str, mode: str, rows_before: int, rows_after: int,
    rows_loaded: int,
    periods: set[str], started_at: str, failure_reason: str | None,
) -> None:
    """Best-effort delivery and durable observation; never changes ingest result."""
    from urllib.parse import urlencode

    from pipeline.scripts.ingest_hook.completion_signal import CompletionSignal, PublishResult, publish

    epoch, category, manifest_sha = identity
    # A retry after load may observe the already-materialized staging rows and
    # otherwise emit zero for the same identity. Freeze the first emitted count
    # tuple so consumers never see one idempotency key with conflicting values.
    try:
        prior_signals = ledger.signal_events(*identity)
    except Exception:  # signal observation is best-effort
        prior_signals = []
    if prior_signals:
        try:
            prior_payload = prior_signals[0].payload
            rows_before = int(prior_payload["rows_before"])
            rows_after = int(prior_payload["rows_after"])
            rows_loaded = int(prior_payload["rows_loaded"])
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[signal] prior payload invalid (ignored): {type(exc).__name__}: {exc}", file=sys.stderr)
    query = urlencode({"epoch": epoch, "category": category, "manifest_sha": manifest_sha})
    signal = CompletionSignal(
        event=event, mode=mode, category=category, epoch=epoch,
        manifest_sha=manifest_sha, rows_before=rows_before, rows_after=rows_after,
        rows_loaded=rows_loaded, period_from=min(periods) if periods else None,
        period_to=max(periods) if periods else None, started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(), failure_reason=failure_reason,
        log_ref=f"/ingest/status?{query}",
    )
    try:
        endpoint, attempts = config.completion_webhook()
        result = publish(signal, endpoint=endpoint, attempts=attempts)
    except Exception as exc:  # malformed delivery config is also non-fatal
        result = PublishResult("failed", 0, f"{type(exc).__name__}: {exc}")
    try:
        ledger.record_signal(
            *identity, run_id=run_id, event=event, mode=mode,
            rows_loaded=rows_loaded, delivery_status=result.status,
            attempts=result.attempts, reason=result.reason, payload=signal.as_dict(),
        )
    except Exception as exc:  # signal observation cannot break a successful load
        print(f"[signal] ledger record failed (ignored): {type(exc).__name__}: {exc}", file=sys.stderr)
    # Stage observation is also best-effort (record_stage swallows DB errors).
    tracker.complete("signal", reason=f"delivery={result.status}; attempts={result.attempts}")
    print(f"signal event={event} mode={mode} delivery={result.status} attempts={result.attempts}")


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
    entry = ledger.status(*identity)
    if entry is None:
        # Standalone/sweep execution: register the identity before running.
        ledger.receive(*identity, manifest_path=str(manifest_path), uploaded_by=manifest.uploaded_by)
        entry = ledger.status(*identity)
    if entry.status == STATUS_COMPLETE:
        # Defence in depth: a re-delivered Job for a completed identity is a no-op.
        print(f"result=noop reason=identity already complete epoch={manifest.epoch} category={manifest.category}")
        return 0
    if entry.status == STATUS_QUEUED:
        ledger.mark_running(*identity, job_name=os.environ.get("HOSTNAME", f"local-{run_id}"), run_id=run_id)

    tracker = _StageTracker(ledger, identity, run_id)
    mode = "staging" if rehearsal_root is not None else str(config.load_mode())
    rows_before = 0
    rows_after = 0
    rows_loaded = 0
    periods: set[str] = set()
    writer_conn = None
    mart_conn = None
    corpus_candidate = None
    mart_activation = None
    writer_lock_acquired = False
    writer_lock_name = None
    publish_actions: tuple[object, ...] = ()
    activation_succeeded = False
    ledger_completed = False
    completion_signal_emitted = False
    baseline_live_snapshot = None
    baseline_manifest_sha = None
    activation_journal = None
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
            if spec.sigma_source and not configured_staging_verify:
                source_db = mart_activation.source_db if mart_activation is not None else None
                baseline_conn = config.open_mart_connection(source_db)
                baseline_lock_acquired = False
                try:
                    if manifest.category == "ubist" and is_shadow:
                        recovery_lock_name = ubist_mart_activation.shadow_lock_name(
                            mart_activation.target_db
                        )
                        recovery_lock_acquired = False
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
                        finally:
                            if recovery_lock_acquired:
                                ubist_mart_activation.release_writer_lock(
                                    baseline_conn, lock_name=recovery_lock_name
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
                            _run_commands("refresh", spec.refresh_argv)
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
                finally:
                    if baseline_lock_acquired:
                        ubist_mart_activation.release_writer_lock(baseline_conn)
                    baseline_conn.close()
            if manifest.category == "ubist" and not configured_staging_verify:
                corpus_candidate = ubist_mart_activation.prepare_candidate_corpus(
                    target_root / "ubist", run_id=run_id
                )
            # 2) real load — wire the materialized upload in, prove the epoch landed (M-2).
            tracker.enter("load")
            load_result = _real_load(
                manifest,
                spec,
                input_root,
                target_dir_override=(corpus_candidate.candidate_root if corpus_candidate else None),
                run_id=run_id,
            )
            rows_before = int(load_result.get("rows_before") or 0)
            rows_after = int(load_result.get("epoch_rows") or 0)
            rows_loaded = int(load_result.get("rows_loaded") or 0)
            tracker.done()
            if load_result["epoch_rows"] is not None:
                tracker.complete("load_verify")  # verify_epoch_loaded ran inside _real_load
            else:
                tracker.skip("load_verify", "category has no load_verify spec")
            staging_verify = load_result["staging_verify"]
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
                        else None
                    )
                    print(
                        f"phase=mart_build status=start build_db={mart_activation.build_db} "
                        f"catalog_root={catalog_root} ubist_dir={load_result['target_dir']}"
                    )
                    ubist_mart_activation.build_shadow(
                        mart_activation,
                        catalog_root=catalog_root,
                        ubist_dir=load_result["target_dir"],
                    )
                    mart_conn = config.open_mart_connection(mart_activation.build_db)
                    print(f"phase=mart_build status=complete build_db={mart_activation.build_db}")
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
                    post = run_post_gates(
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
                    print(f"gate=post status={post.status} duration_ms={post.duration_ms}")
                    tracker.done()
                    tracker.complete("sigma")  # sigma_check ran inside run_post_gates and passed
                else:
                    tracker.skip("sigma", "category has no sigma_source")
                    tracker.skip("post_gate", "category has no sigma_source")

                # 5) Only a gated candidate may replace corpus + serving mart.
                if mart_activation is not None:
                    from pipeline.scripts.ingest_hook import ubist_mart_activation

                    tracker.enter("mart_publish")
                    writer_db = mart_activation.target_db if is_shadow else None
                    writer_conn = config.open_mart_connection(writer_db)
                    writer_lock_name = (
                        ubist_mart_activation.shadow_lock_name(mart_activation.target_db)
                        if is_shadow
                        else ubist_mart_activation.WRITER_LOCK_NAME
                    )
                    ubist_mart_activation.acquire_writer_lock(
                        writer_conn, timeout_seconds=0, lock_name=writer_lock_name
                    )
                    writer_lock_acquired = True
                    ubist_mart_activation.require_writer_lock_owner(
                        writer_conn, lock_name=writer_lock_name
                    )
                    ubist_mart_activation.require_corpus_manifest(
                        corpus_candidate.live_root, baseline_manifest_sha
                    )
                    snapshot_conn = config.open_mart_connection(mart_activation.source_db)
                    try:
                        current_live_snapshot = fingerprint_untouched_sources(
                            snapshot_conn, touched_source="__jw_ingest_no_source__"
                        )
                    finally:
                        snapshot_conn.close()
                    if current_live_snapshot != baseline_live_snapshot:
                        raise RuntimeError("serving general mart changed while candidate was built")
                    activation_journal = ubist_mart_activation.write_activation_journal(
                        corpus_candidate,
                        mart_activation,
                        run_id=run_id,
                        phase="prepared",
                        identity=identity,
                    )
                    ubist_mart_activation.promote_candidate_corpus(corpus_candidate)
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "corpus_promoted"
                    )
                    if is_shadow:
                        ubist_mart_activation.maybe_inject_shadow_crash(
                            "after_corpus_publish"
                        )
                    print(f"phase=mart_publish status=start build_db={mart_activation.build_db}")
                    publish_actions = ubist_mart_activation.publish_shadow(
                        writer_conn,
                        mart_activation,
                        run_id=run_id,
                        epoch=manifest.epoch,
                        ingest_run_id=run_id,
                        require_ledger_gate=not is_shadow,
                    )
                    ubist_mart_activation.update_activation_journal(
                        activation_journal, "mart_promoted"
                    )
                    if is_shadow:
                        ubist_mart_activation.maybe_inject_shadow_crash(
                            "after_mart_publish"
                        )
                    print(f"phase=mart_publish status=complete tables={len(publish_actions)}")
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
            )
        print(f"result=complete epoch={manifest.epoch} category={manifest.category} run_id={run_id}")
        return 0
    except PostGateError as exc:
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
        if mart_activation is not None and not activation_succeeded:
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
                            _run_commands("refresh", resolve_category("ubist").refresh_argv)
                    except Exception as recovery_exc:
                        print(
                            f"recovery=deferred reason={type(recovery_exc).__name__}: {recovery_exc}",
                            file=sys.stderr,
                        )
                    else:
                        ubist_mart_activation.complete_recovery(recovered)
            elif corpus_candidate is not None:
                ubist_mart_activation.rollback_candidate_corpus(corpus_candidate)
        if writer_lock_acquired and writer_conn is not None:
            from pipeline.scripts.ingest_hook import ubist_mart_activation

            ubist_mart_activation.release_writer_lock(
                writer_conn, lock_name=writer_lock_name
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
