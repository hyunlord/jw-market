#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# --- How to run ---
# uv run verify_ocr_psm_gate.py --summary /path/to/summary.json
# ------------------

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
from typing import Final, TypedDict


TARGET_PSM: Final = 4
TARGET_DPI: Final = 200
TARGET_WORKERS: Final = 4


class TimingRow(TypedDict):
    engine: str
    tesseract_psm: int
    dpi: int
    workers: int
    pages: int


class AccuracyRow(TypedDict, total=False):
    engine: str
    tesseract_psm: int
    dpi: int
    workers: int
    group: str
    pages: int
    cer_macro: float
    numeric_error_rate: float
    numeric_matched: int
    numeric_total: int


class Summary(TypedDict):
    timing: list[TimingRow]
    accuracy: list[AccuracyRow]


def _is_target(row: TimingRow | AccuracyRow) -> bool:
    return (
        row["engine"] == "tesseract"
        and row["tesseract_psm"] == TARGET_PSM
        and row["dpi"] == TARGET_DPI
        and row["workers"] == TARGET_WORKERS
    )


def _numeric_error(rows: list[AccuracyRow]) -> float:
    totals = [int(row.get("numeric_total", 0)) for row in rows]
    if sum(totals) > 0:
        matched = sum(int(row.get("numeric_matched", 0)) for row in rows)
        return (sum(totals) - matched) / sum(totals)
    korean = next((row for row in rows if row.get("group") == "ko"), None)
    return float(korean.get("numeric_error_rate", 1.0)) if korean else 1.0


def evaluate(summary: Summary, *, max_ko_cer: float, max_numeric_error: float) -> int:
    timing_rows = [row for row in summary.get("timing", []) if _is_target(row)]
    accuracy_rows = [row for row in summary.get("accuracy", []) if _is_target(row)]
    population = timing_rows[0]["pages"] if len(timing_rows) == 1 else 0
    checked = sum(int(row.get("pages", 0)) for row in accuracy_rows)
    korean = next((row for row in accuracy_rows if row.get("group") == "ko"), None)
    failures = 0
    failures += int(population == 0 or checked == 0 or checked != population)
    failures += int(korean is None or float(korean.get("cer_macro", 1.0)) > max_ko_cer)
    failures += int(_numeric_error(accuracy_rows) > max_numeric_error)
    exit_code = int(failures > 0)
    print("gate=G1-ocr-accuracy")
    print("classification=sample")
    print(f"checked={checked}")
    print(f"population={population}")
    print("missing=fail")
    print("tolerance=absolute")
    print(f"failures={failures}")
    print(f"exit_code={exit_code}")
    print(f"environment={platform.node() or 'runtime'}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--max-ko-cer", type=float, default=0.08)
    parser.add_argument("--max-numeric-error", type=float, default=0.09)
    arguments = parser.parse_args()
    summary: Summary = json.loads(arguments.summary.read_text(encoding="utf-8"))
    return evaluate(
        summary,
        max_ko_cer=arguments.max_ko_cer,
        max_numeric_error=arguments.max_numeric_error,
    )


if __name__ == "__main__":
    raise SystemExit(main())
