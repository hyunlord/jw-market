#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from scripts.shadow_archive_provenance import finalize_archive
from scripts.shadow_transition_contract import (
    TransitionObservation,
    classify_rc,
    record_outcome,
    validate_outcome_files,
)


def _bool_argument(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return lowered == "true"


def _json_text(value: dict[str, str | int | bool | None]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed SHADOW transition outcome recorder")
    commands = parser.add_subparsers(dest="command", required=True)

    classify = commands.add_parser("classify")
    classify.add_argument("--rc", type=int, required=True)

    record = commands.add_parser("record")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--rc", type=int, required=True)
    record.add_argument("--patched", type=_bool_argument, required=True)
    record.add_argument("--rolled-back", type=_bool_argument, required=True)
    record.add_argument("--observed-mode", required=True)
    record.add_argument("--target-mode", default="SHADOW")

    validate = commands.add_parser("validate")
    validate.add_argument("--output", type=Path, required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--source", type=Path, required=True)
    finalize.add_argument("--archive", type=Path, required=True)
    finalize.add_argument("--rebuild-reason")
    return parser


def main() -> int:
    args = _parser().parse_args()
    match args.command:
        case "classify":
            payload = asdict(classify_rc(args.rc))
        case "record":
            payload = asdict(
                record_outcome(
                    args.output,
                    rc=args.rc,
                    observation=TransitionObservation(
                        patched=args.patched,
                        rolled_back=args.rolled_back,
                        observed_mode=args.observed_mode,
                        target_mode=args.target_mode,
                    ),
                )
            )
        case "validate":
            payload = asdict(validate_outcome_files(args.output))
        case "finalize":
            payload = asdict(
                finalize_archive(
                    args.source,
                    args.archive,
                    rebuild_reason=args.rebuild_reason,
                )
            )
        case unexpected:
            raise AssertionError(f"unsupported parser command: {unexpected}")
    print(_json_text(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
