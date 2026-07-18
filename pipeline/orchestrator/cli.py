"""CLI: ``python -m pipeline.orchestrator run [options]``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.orchestrator.executor import EventLog, execute_plan
from pipeline.orchestrator.planner import MODES, build_plan
from pipeline.orchestrator.stages import STAGE_BY_KEY, STAGE_ORDER
from pipeline.orchestrator.state import StateStore, default_state_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline.orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="plan and execute the pipeline chain")
    run.add_argument("--mode", choices=MODES, default="full")
    run.add_argument("--stages", help=f"comma-separated subset of {list(STAGE_ORDER)}")
    run.add_argument("--from-stage", help="start at this stage (upstream must be completed at the current epoch)")
    run.add_argument("--brands", help="comma-separated brand scope (incremental special form)")
    run.add_argument("--dry-run", action="store_true", help="print the plan only; execute nothing, write nothing")
    run.add_argument(
        "--force-plan",
        action="store_true",
        help="with --dry-run: include full commands even for stages that would be skipped as fresh",
    )
    run.add_argument("--force", action="store_true", help="override freshness and stale-dependency checks (recorded)")
    run.add_argument("--state-file", type=Path, default=None)
    run.add_argument("--log-file", type=Path, default=None)
    run.add_argument("--run-id", default=None)

    rehearsal = sub.add_parser(
        "rehearse-full",
        help="rebuild explicit raw inputs into isolated mart/cache schemas (never publish)",
    )
    rehearsal.add_argument("--input-manifest", required=True, type=Path)
    rehearsal.add_argument("--target-db", required=True)
    rehearsal.add_argument("--cache-db", required=True)
    rehearsal.add_argument("--source-db", required=True)
    rehearsal.add_argument("--work-dir", required=True, type=Path)
    rehearsal.add_argument("--dry-run", action="store_true")

    comparison = sub.add_parser(
        "compare-full",
        help="read-only census comparison of isolated full-rehearsal outputs",
    )
    comparison.add_argument("--reference-db", required=True)
    comparison.add_argument("--target-db", required=True)
    comparison.add_argument("--reference-cache-db", required=True)
    comparison.add_argument("--target-cache-db", required=True)
    comparison.add_argument("--output", required=True, type=Path)

    sub.add_parser("stages", help="print the stage registry and incremental capability table")
    return parser


def _stages_table() -> dict:
    return {
        "stages": [
            {
                "stage": spec.key,
                "deps": list(spec.deps),
                "incremental": spec.incremental,
                "supports_brands": spec.supports_brands,
                "reason": spec.incremental_reason,
                "description": spec.description,
            }
            for spec in (STAGE_BY_KEY[key] for key in STAGE_ORDER)
        ]
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "stages":
        print(json.dumps(_stages_table(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "rehearse-full":
        from pipeline.orchestrator.full_rehearsal import (
            FullRehearsalConfig,
            RehearsalContractError,
            execute_full_rehearsal,
        )

        try:
            return execute_full_rehearsal(
                FullRehearsalConfig(
                    input_manifest=args.input_manifest,
                    target_db=args.target_db,
                    cache_db=args.cache_db,
                    source_db=args.source_db,
                    work_dir=args.work_dir,
                ),
                dry_run=args.dry_run,
            )
        except RehearsalContractError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.command == "compare-full":
        from pipeline.orchestrator.full_rehearsal_compare import (
            ComparisonConfig,
            run_comparison,
        )

        try:
            return run_comparison(
                ComparisonConfig(
                    reference_db=args.reference_db,
                    target_db=args.target_db,
                    reference_cache_db=args.reference_cache_db,
                    target_cache_db=args.target_cache_db,
                ),
                args.output,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    state = StateStore(args.state_file or default_state_path())

    from pipeline.orchestrator.probe import MartProbe

    probe = MartProbe()
    brands = tuple(brand.strip() for brand in (args.brands or "").split(",") if brand.strip())

    try:
        plan = build_plan(
            mode=args.mode,
            run_id=run_id,
            probe=probe,
            state=state,
            stages_csv=args.stages,
            from_stage=args.from_stage,
            brands=brands,
            force=args.force or (args.dry_run and args.force_plan),
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log = EventLog(run_id, log_file=args.log_file)
    return execute_plan(plan, state, log, dry_run=args.dry_run)
