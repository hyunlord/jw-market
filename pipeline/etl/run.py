"""Entrypoint for the new ETL skeleton."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.etl.stages import (  # noqa: E402
    s0_verify,
    s1_load,
    s2_catalog,
    s3_enrich,
    s4_mart,
    s5_mart,
    s6_cache,
    s7_bridge,
)

STAGES = [s0_verify, s1_load, s2_catalog, s3_enrich, s4_mart, s5_mart, s6_cache, s7_bridge]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ETL skeleton.")
    parser.add_argument("--all", action="store_true", help="Run all skeleton stages.")
    parser.add_argument("--period", help="Run all stages for YYYY-MM or YYYY-Qn.")
    parser.add_argument(
        "--apply-change",
        action="store_true",
        help="Run all stages in apply-change skeleton mode.",
    )
    parser.add_argument(
        "--record-baseline",
        action="store_true",
        help="Manually record the s0 file manifest baseline.",
    )
    parser.add_argument(
        "--target-dir",
        help="Override the parquet target directory for load stages.",
    )
    parser.add_argument("--file", help="Load or dry-run one source file for s1 smoke checks.")
    parser.add_argument(
        "--source-file",
        action="append",
        default=[],
        help="Repeatable source file for an atomic full-source s1 load.",
    )
    parser.add_argument("--input-file", help="Override the stage input file when supported.")
    parser.add_argument("--mi-master", help="Explicit MI Master xlsx path for s2 catalog stages.")
    parser.add_argument("--catalog-path", help="Override the s2 catalog mapping config.")
    parser.add_argument("--cache-dir", help="Override the s2 seed cache directory when supported.")
    parser.add_argument("--inputs-dir", help="Override the s2 auxiliary inputs directory when supported.")
    parser.add_argument("--env-file", help="Load DB environment values from an explicit .env file.")
    parser.add_argument("--audit-dir", help="Override the stage audit directory when supported.")
    parser.add_argument("--catalog-root", help="Override the catalog parquet root when supported.")
    parser.add_argument("--ubist-dir", help="Override the UBIST parquet root when supported.")
    parser.add_argument("--ubist-source-dir", help="Explicit raw UBIST xlsx root for isolated full loads.")
    parser.add_argument("--iqvia-source-dir", help="Explicit raw IQVIA NSA root for isolated full loads.")
    parser.add_argument("--iqvia-nsa-dir", help="Override the canonical IQVIA NSA parquet root when supported.")
    parser.add_argument("--enriched-dir", help="Override the layer2 enriched parquet root when supported.")
    parser.add_argument("--input-mode", choices=["raw", "enriched"], default="raw", help="S4 general mart input surface.")
    parser.add_argument("--limit-atc4", type=int, help="Limit S4 processing to the first N ATC4 codes for smoke checks.")
    parser.add_argument("--max-rows", type=int, help="Limit raw input rows for supported smoke checks.")
    parser.add_argument("--spool-dir", help="Durable working directory for bounded S4 UBIST partitions.")
    parser.add_argument(
        "--memory-budget-bytes",
        type=int,
        help="Memory budget used to size S4 UBIST partitions.",
    )
    parser.add_argument("--ml-id", help="Run one market id when supported.")
    parser.add_argument(
        "--cache-cause-mode",
        choices=["full-all-brands", "serving-slim"],
        default="full-all-brands",
        help="S6 cache_cause output mode; production default is full-all-brands.",
    )
    parser.add_argument("--truncate", action="store_true", help="Remove target output before supported stage loads.")
    parser.add_argument("--ingested-at", help="Override s2 extract timestamp for deterministic parity checks.")
    parser.add_argument(
        "--source",
        choices=["ubist", "iqvia", "nsa", "all"],
        default="all",
        help="Source dispatcher for s1.",
    )
    parser.add_argument(
        "--target-db",
        help="Temporary target database for s1 IQVIA verification loads.",
    )
    parser.add_argument(
        "--source-db",
        default="jw_mart",
        help="Source database to clone raw table definitions from for s1 IQVIA.",
    )
    parser.add_argument(
        "--strategic-source-db",
        help="Source database for strategic mart inputs when supported.",
    )
    parser.add_argument(
        "--event-source-db",
        default="jw_mart",
        help="Source database for event inputs when supported.",
    )
    parser.add_argument(
        "--record-parquet-dir",
        help="Override the IQVIA record parquet cache directory for s1.",
    )
    parser.add_argument("--batch-size", type=int, default=10000, help="Batch size for s1 source loaders.")
    parser.add_argument("--dry-run", action="store_true", help="Run source dispatch without writing when supported.")
    parser.add_argument(
        "--sync-catalog-db",
        action="store_true",
        help="For s2, upsert finalized output/catalog parquet into catalog_* tables.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="For UBIST s1, merge every selected workbook by business row identity.",
    )
    parser.add_argument(
        "--ubist-mode",
        choices=["replace", "append"],
        default="replace",
        help="UBIST parquet write mode for s1.",
    )
    parser.add_argument(
        "--exclude-ubist-month",
        action="append",
        default=[],
        metavar="YYYY-MM",
        dest="exclude_ubist_month",
        help=(
            "Skip these UBIST periods during s1 load (they are pinned to canonical "
            "parquet sidecars installed by a later rehearsal step). Repeatable."
        ),
    )
    parser.add_argument("--stage", choices=[stage.STAGE.split()[0] for stage in STAGES])
    args = parser.parse_args(argv)
    if args.input_file and args.mi_master:
        parser.error("--input-file and --mi-master are aliases; pass only one")
    for month in args.exclude_ubist_month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            parser.error(f"--exclude-ubist-month must be YYYY-MM: {month!r}")
    return args


def load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"--env-file not found: {env_path}")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def select_stages(args: argparse.Namespace) -> list[Any]:
    if args.stage:
        return [stage for stage in STAGES if stage.STAGE.startswith(args.stage)]
    return STAGES


def mode_name(args: argparse.Namespace) -> str:
    if args.stage:
        return f"stage:{args.stage}"
    if args.period:
        return "period"
    if args.apply_change:
        return "apply-change"
    if args.all:
        return "all"
    return "all"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        load_env_file(args.env_file)
    except Exception as exc:
        print(f"[etl] 실패 env-file={args.env_file}: {exc}")
        return 1
    params: dict[str, Any] = {
        "period": args.period,
        "apply_change": args.apply_change,
        # s0 only reports file-manifest facts. Automatic skip/incremental
        # decisions belong here in run.py once s1-s6 perform real work; phase
        # 1B exposes only this manual recording switch.
        "record_baseline": args.record_baseline,
        "target_dir": args.target_dir,
        "file": args.file,
        "source_files": args.source_file,
        "input_file": args.mi_master or args.input_file,
        "catalog_path": args.catalog_path,
        "cache_dir": args.cache_dir,
        "inputs_dir": args.inputs_dir,
        "env_file": args.env_file,
        "audit_dir": args.audit_dir,
        "catalog_root": args.catalog_root,
        "ubist_dir": args.ubist_dir,
        "ubist_source_dir": args.ubist_source_dir,
        "iqvia_source_dir": args.iqvia_source_dir,
        "iqvia_nsa_dir": args.iqvia_nsa_dir,
        "enriched_dir": args.enriched_dir,
        "input_mode": args.input_mode,
        "limit_atc4": args.limit_atc4,
        "max_rows": args.max_rows,
        "spool_dir": args.spool_dir,
        "memory_budget_bytes": args.memory_budget_bytes,
        "ml_id": args.ml_id,
        "cache_cause_mode": args.cache_cause_mode,
        "truncate": args.truncate,
        "ingested_at": args.ingested_at,
        "source": args.source,
        "target_db": args.target_db,
        "source_db": args.source_db,
        "strategic_source_db": args.strategic_source_db,
        "event_source_db": args.event_source_db,
        "record_parquet_dir": args.record_parquet_dir,
        "batch_size": args.batch_size,
        "dry_run": args.dry_run,
        "sync_catalog_db": args.sync_catalog_db,
        "incremental": args.incremental,
        "ubist_mode": args.ubist_mode,
        "exclude_ubist_months": list(args.exclude_ubist_month),
        "mode": mode_name(args),
    }
    print(f"[etl] 모드={params['mode']} period={params['period']}")
    for stage in select_stages(args):
        rc = stage.run(params)
        if rc != 0:
            print(f"[etl] 실패 stage={stage.STAGE} rc={rc}")
            return int(rc)
    print("[etl] 완료 rc=0")
    # Success-path only: wake the orchestrator (opt-in via env; no-op otherwise).
    # A failed load returns above and never kicks, so stale data cannot propagate.
    from pipeline.etl.kick import maybe_kick_orchestrator

    maybe_kick_orchestrator(params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
