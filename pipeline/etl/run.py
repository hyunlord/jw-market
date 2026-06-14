"""Entrypoint for the new ETL skeleton."""
from __future__ import annotations

import argparse
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
    s3_postfix,
    s4_enrich,
    s5_mart,
    s6_cache,
)

STAGES = [s0_verify, s1_load, s2_catalog, s3_postfix, s4_enrich, s5_mart, s6_cache]


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
    parser.add_argument("--input-file", help="Override the stage input file when supported.")
    parser.add_argument("--catalog-path", help="Override the s2 catalog mapping config.")
    parser.add_argument("--ingested-at", help="Override s2 extract timestamp for deterministic parity checks.")
    parser.add_argument(
        "--source",
        choices=["ubist", "iqvia", "all"],
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
        "--record-parquet-dir",
        help="Override the IQVIA record parquet cache directory for s1.",
    )
    parser.add_argument("--batch-size", type=int, default=10000, help="Batch size for s1 source loaders.")
    parser.add_argument("--dry-run", action="store_true", help="Run source dispatch without writing when supported.")
    parser.add_argument(
        "--ubist-mode",
        choices=["replace", "append"],
        default="replace",
        help="UBIST parquet write mode for s1.",
    )
    parser.add_argument("--stage", choices=[stage.STAGE.split()[0] for stage in STAGES])
    return parser.parse_args(argv)


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
    params: dict[str, Any] = {
        "period": args.period,
        "apply_change": args.apply_change,
        # s0 only reports file-manifest facts. Automatic skip/incremental
        # decisions belong here in run.py once s1-s6 perform real work; phase
        # 1B exposes only this manual recording switch.
        "record_baseline": args.record_baseline,
        "target_dir": args.target_dir,
        "file": args.file,
        "input_file": args.input_file,
        "catalog_path": args.catalog_path,
        "ingested_at": args.ingested_at,
        "source": args.source,
        "target_db": args.target_db,
        "source_db": args.source_db,
        "record_parquet_dir": args.record_parquet_dir,
        "batch_size": args.batch_size,
        "dry_run": args.dry_run,
        "ubist_mode": args.ubist_mode,
        "mode": mode_name(args),
    }
    print(f"[etl] 모드={params['mode']} period={params['period']}")
    for stage in select_stages(args):
        rc = stage.run(params)
        if rc != 0:
            print(f"[etl] 실패 stage={stage.STAGE} rc={rc}")
            return int(rc)
    print("[etl] 완료 rc=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
