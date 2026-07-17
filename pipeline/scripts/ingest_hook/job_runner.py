"""Entrypoint executed inside the ingest Job.

Order is enforced in code (STOP ③ — no load without G3):
  1. contract parse (fail-closed on unknown category)
  2. G3 structural validation
  3. load phase        (rehearsal: CSV -> sqlite staging; real: pipeline.etl.run)
  4. Σ(parts)=whole gate on the staged data
  5. downstream refresh (real: pipeline.orchestrator --mode incremental)
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
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, STATUS_QUEUED, Ledger
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
        ledger.receive(*identity, manifest_path=str(manifest_path))
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

        # 2) load + 3) Σ gate
        if rehearsal_root is not None:
            table = _rehearsal_load(manifest, input_root, rehearsal_root)
            print(f"gate=sigma status=pass table={table} (rehearsal staging)")
            print("phase=refresh status=skipped reason=rehearsal (orchestrator untouched)")
        else:
            _run_commands("load", spec.load_argv)
            # Σ reconciliation inside the builders (staged promotion + expected
            # counts); category-pinned sigma SQL is an activation-time addition.
            _run_commands("refresh", spec.refresh_argv)

        ledger.mark_complete(*identity, row_counts=report.file_rows)
        print(f"result=complete epoch={manifest.epoch} category={manifest.category} run_id={run_id}")
        return 0
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
    input_root = args.input_root or config.input_root()
    ledger = config.open_configured_ledger()
    return run(args.manifest, input_root=input_root, ledger=ledger, rehearsal_root=rehearsal_root)


if __name__ == "__main__":
    raise SystemExit(main())
