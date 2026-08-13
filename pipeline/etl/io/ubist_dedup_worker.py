#!/usr/bin/env python3
"""Run one UBIST partition dedup in a disposable process."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from pipeline.etl.io.ubist_loader import deduplicate_partition_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--additional-path", type=Path, action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = deduplicate_partition_file(
        args.path,
        args.period,
        additional_paths=tuple(args.additional_path),
    )
    args.result.write_text(
        json.dumps(asdict(report), ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
