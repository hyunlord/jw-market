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
