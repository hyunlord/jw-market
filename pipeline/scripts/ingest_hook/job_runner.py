"""Entrypoint executed inside the ingest Job.

Order is enforced in code (STOP ③ — no load without G3):
  1. contract parse (fail-closed on unknown category)
  2. G3 structural validation
  3. load phase        (rehearsal: CSV -> sqlite staging; real: pipeline.etl.run)
  4. POST-GATE (Σ, manifest row coverage, untouched-source fingerprint)
  5. downstream refresh (real: pipeline.orchestrator --mode incremental)
  6. ledger complete
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
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


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


def _real_load(manifest, spec, input_root: Path) -> dict:
    """Wire the materialized upload into the loader, run it, and prove the epoch
    landed (M-2). Returns {target_dir, epoch_rows, staging_verify}.

    Fail-closed rules:
      * a category with a load_argv but no load_input_flag is UNWIRED — refuse
        to run it in real mode (it would load unrelated defaults = silent failure).
      * the epoch must appear in the loader's own output with rows > 0.
    """
    from pipeline.scripts.ingest_hook.load_verify import verify_epoch_loaded

    if not spec.load_argv:
        return {"target_dir": None, "epoch_rows": None, "staging_verify": None}  # e.g. skeleton

    if not spec.load_input_flag:
        raise RuntimeError(
            f"category {manifest.category!r} has a load command but no upload wiring "
            "(load_input_flag); refusing to load unrelated defaults (silent-failure guard)"
        )

    target_root, staging_verify = config.load_output_root()
    target_dir = target_root / manifest.category / manifest.epoch
    _seed_empty_manifest(target_dir, spec.load_verify)

    read_files = [str((input_root / entry.path).resolve()) for entry in manifest.files]
    for source in read_files:
        argv = list(spec.load_argv)
        argv.extend([spec.load_input_flag, source])
        if spec.load_target_flag:
            argv.extend([spec.load_target_flag, str(target_dir)])
        print(f"phase=load reading={source} target={target_dir} staging_verify={staging_verify}")
        _run_commands("load", tuple(argv))

    # M-2: the uploaded epoch must be present in the loader's output.
    epoch_rows = None
    if spec.load_verify:
        epoch_rows = verify_epoch_loaded(spec.load_verify, target_dir, manifest.epoch)
        print(f"gate=load_verify status=pass epoch={manifest.epoch} rows={epoch_rows} target={target_dir}")

    return {"target_dir": target_dir, "epoch_rows": epoch_rows, "staging_verify": staging_verify}


def run(manifest_path: Path, *, input_root: Path, ledger: Ledger, rehearsal_root: Path | None) -> int:
    run_id = _run_id()
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

    try:
        spec = resolve_category(manifest.category)
        previous_total = ledger.previous_complete_total(manifest.category, before_epoch=manifest.epoch)

        # 1) G3 — always first; a failure here has zero DB effect.
        report = validate(manifest, spec, input_root, previous_total_rows=previous_total)
        print(f"gate=g3 status=pass files={len(report.file_rows)} rows={report.total_rows}")

        # 2) load + 3) fail-closed post-gates
        if rehearsal_root is not None:
            table = _rehearsal_load(manifest, input_root, rehearsal_root)
            conn = sqlite3.connect(str(rehearsal_root / "staging.db"))
            try:
                stable = SourceSnapshot((TableFingerprint("external_mart", 0, "untouched"),))

                def rehearsal_sigma() -> SigmaEvidence:
                    sigma = check_staging(conn, table)
                    checked = len(sigma.periods)
                    return SigmaEvidence(checked, checked, str(sigma.periods))

                post = run_post_gates(
                    run_id=run_id,
                    epoch=manifest.epoch,
                    category=manifest.category,
                    sigma_check=rehearsal_sigma,
                    expected_rows=report.total_rows,
                    actual_rows=staging_row_count(conn, table),
                    untouched_before=stable,
                    untouched_after=stable,
                    report_path=rehearsal_root / "post_gate_report.json",
                )
            finally:
                conn.close()
            print(f"gate=post status={post.status} duration_ms={post.duration_ms}")
            print("phase=refresh status=skipped reason=rehearsal (orchestrator untouched)")
        else:
            mart_conn = None
            before_snapshot = None
            _, configured_staging_verify = config.load_output_root()
            if spec.sigma_source and not configured_staging_verify:
                mart_conn = config.open_mart_connection()
                before_snapshot = fingerprint_untouched_sources(
                    mart_conn, touched_source=spec.sigma_source
                )
            # 2) real load — wire the materialized upload in, prove the epoch landed (M-2).
            load_result = _real_load(manifest, spec, input_root)
            staging_verify = load_result["staging_verify"]
            if staging_verify:
                # Isolated J5 verification: real loader exercised, zero mart write.
                print("gate=sigma status=skipped reason=staging-verify (mart untouched)")
                print("phase=refresh status=skipped reason=staging-verify (orchestrator untouched)")
            else:
                # 3) Σ gate + downstream refresh only when writing to the live mart.
                if spec.sigma_source:
                    from pipeline.scripts.ingest_hook.sigma_market import check_market_sigma

                    affected = tuple(sorted(report.observed_periods)) or (manifest.epoch,)
                    sampled = sample_existing_periods(
                        mart_conn, source=spec.sigma_source, excluded=affected
                    )
                    periods = tuple(sorted(set(affected + sampled)))

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
                    post = run_post_gates(
                        run_id=run_id,
                        epoch=manifest.epoch,
                        category=manifest.category,
                        sigma_check=mart_sigma,
                        expected_rows=report.total_rows,
                        actual_rows=int(load_result["epoch_rows"] or 0),
                        untouched_before=before_snapshot,
                        untouched_after=fingerprint_untouched_sources(
                            mart_conn, touched_source=spec.sigma_source
                        ),
                        report_path=load_result["target_dir"] / "post_gate_report.json",
                    )
                    print(f"gate=post status={post.status} duration_ms={post.duration_ms}")
                _run_commands("refresh", spec.refresh_argv)
            if load_result["epoch_rows"] is not None:
                report.file_rows[f"epoch:{manifest.epoch}"] = load_result["epoch_rows"]

        ledger.mark_complete(*identity, row_counts=report.file_rows)
        print(f"result=complete epoch={manifest.epoch} category={manifest.category} run_id={run_id}")
        return 0
    except PostGateError as exc:
        ledger.mark_gate_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        print(f"result=gate_failed reason={exc}", file=sys.stderr)
        return 1
    except (G3Error, SigmaGateError, UnknownCategoryError, RuntimeError) as exc:
        ledger.mark_failed(*identity, reason=f"{type(exc).__name__}: {exc}")
        print(f"result=failed reason={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.scripts.ingest_hook.job_runner")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--input-root", type=Path, default=None)
    parser.add_argument("--rehearsal-root", type=Path, default=None)
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
        return run(local_manifest, input_root=workdir, ledger=ledger, rehearsal_root=rehearsal_root)

    input_root = args.input_root or config.input_root()
    return run(args.manifest, input_root=input_root, ledger=ledger, rehearsal_root=rehearsal_root)


if __name__ == "__main__":
    raise SystemExit(main())
