"""Entrypoint executed inside the ingest Job.

Order is enforced in code (STOP ③ — no load without G3):
  1. contract parse (fail-closed on unknown category)
  2. G3 structural validation
  3. load phase        (rehearsal: CSV -> sqlite staging; real: pipeline.etl.run)
  4. downstream refresh (real: pipeline.orchestrator --mode incremental)
  5. Σ(parts)=whole gate on the refreshed mart
  6. ledger complete
Any failure marks the ledger row failed with the reason and exits non-zero;
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
from pipeline.scripts.ingest_hook.g3 import G3Error, G3Report, validate
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, STATUS_QUEUED, Ledger
from pipeline.scripts.ingest_hook.sigma_market import MarketSigmaError
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
        check_staging(conn, table)
    finally:
        conn.close()
    return table


def _run_commands(label: str, argv: tuple[str, ...]) -> None:
    if not argv:
        return
    result = subprocess.run(argv, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{label} command failed rc={result.returncode}: {' '.join(argv)}")


def _build_load_argv(spec, manifest, input_root: Path) -> tuple[str, ...]:
    """Bind a validated submission to the exact files consumed by s1."""
    if not spec.load_argv:
        return ()
    if manifest.category != "ubist":
        return spec.load_argv

    target = config.ubist_target_dir()
    argv = (*spec.load_argv, "--target-dir", str(target))
    for entry in manifest.files:
        path = (input_root / entry.path).resolve()
        if path.suffix.lower() != ".xlsx":
            raise RuntimeError(f"UBIST real load requires xlsx manifest files: {entry.path}")
        argv = (*argv, "--ubist-file", str(path))
    return argv


def _validate_and_load(
    manifest,
    spec,
    input_root: Path,
    *,
    previous_total_rows: int | None,
    rehearsal_root: Path | None,
) -> G3Report:
    """Run the mandatory G3 boundary and only then consume its exact files."""
    report = validate(
        manifest,
        spec,
        input_root,
        previous_total_rows=previous_total_rows,
    )
    print(f"gate=g3 status=pass files={len(report.file_rows)} rows={report.total_rows}")
    if rehearsal_root is not None:
        table = _rehearsal_load(manifest, input_root, rehearsal_root)
        print(f"gate=sigma status=pass table={table} (rehearsal staging)")
        return report
    _run_commands("load", _build_load_argv(spec, manifest, input_root))
    return report


def _check_market_sigma(spec, report: G3Report) -> None:
    if not spec.sigma_source:
        return
    periods = tuple(sorted(report.observed_periods)) or (report.epoch,)
    conn = config.open_mart_connection()
    try:
        from pipeline.scripts.ingest_hook.sigma_market import check_market_sigma

        sigma = check_market_sigma(conn, source=spec.sigma_source, periods=periods)
    finally:
        conn.close()
    print(
        f"gate=sigma status=pass source={spec.sigma_source} "
        f"markets={sigma.markets_checked} cells={sigma.cells_checked} "
        f"worst_rel={sigma.worst_rel:.6%}"
    )


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

        # 1) G3 + 2) exact load. A G3 failure has zero DB effect.
        report = _validate_and_load(
            manifest,
            spec,
            input_root,
            previous_total_rows=previous_total,
            rehearsal_root=rehearsal_root,
        )

        if rehearsal_root is not None:
            print("phase=refresh status=skipped reason=rehearsal (orchestrator untouched)")
        else:
            # 3) refresh + 4) Σ gate. Sigma must observe the refreshed mart.
            _run_commands("refresh", spec.refresh_argv)
            _check_market_sigma(spec, report)

        ledger.mark_complete(*identity, row_counts=report.file_rows)
        print(f"result=complete epoch={manifest.epoch} category={manifest.category} run_id={run_id}")
        return 0
    except (
        G3Error,
        MarketSigmaError,
        SigmaGateError,
        UnknownCategoryError,
        RuntimeError,
    ) as exc:
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
