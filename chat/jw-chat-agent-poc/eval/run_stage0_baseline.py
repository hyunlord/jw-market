#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "openpyxl",
#     "pyyaml",
# ]
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run eval/run_stage0_baseline.py --raw-results-jsonl /tmp/raw.jsonl --output-dir /tmp/stage0
# 3. Or make executable and run:
#      chmod +x eval/run_stage0_baseline.py && ./eval/run_stage0_baseline.py --help
# ──────────────────

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from stage0_lib.io import load_questions, load_raw_results, write_json
from stage0_lib.model import JsonValue, ScoredRow
from stage0_lib.scoring import score_question
from stage0_lib.workbook import write_workbook


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build Stage 0 baseline workbook.")
    parser.add_argument("--questions", type=Path, default=Path("eval/stage0_questions.yaml"))
    parser.add_argument("--pl-questions", type=Path, default=Path("eval/pl_questions.yaml"))
    parser.add_argument("--raw-results-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--system-label", default="ux-p2-bottom")
    parser.add_argument("--previous-baseline-json", type=Path)
    parser.add_argument("--previous-version", default="v1")
    return parser


def _row_to_json(row: ScoredRow) -> dict[str, JsonValue]:
    return {
        "id": row.question.question_id,
        "category": row.question.category,
        "question": row.question.question,
        "gold_note": row.question.gold_note,
        "answer": row.answer,
        "numeric_accuracy": row.numeric_accuracy,
        "qualitative_score": row.qualitative_score,
        "note": row.note,
        "ok": row.ok,
        "gold_observations": [
            {
                "label": item.label,
                "key": item.key,
                "kind": item.kind,
                "value": item.value,
            }
            for item in row.gold_observations
        ],
    }


def _summary_payload(rows: list[ScoredRow], version: str, system_label: str) -> dict[str, JsonValue]:
    categories = sorted({row.question.category for row in rows})
    by_category: dict[str, JsonValue] = {}
    for category in categories:
        category_rows = [row for row in rows if row.question.category == category]
        numeric = [row.numeric_accuracy for row in category_rows]
        by_category[category] = {
            "count": len(category_rows),
            "numeric_o": numeric.count("O"),
            "numeric_x": numeric.count("X"),
            "numeric_na": numeric.count("NA"),
            "qualitative_avg": round(
                sum(row.qualitative_score for row in category_rows) / len(category_rows), 2
            ),
        }
    return {
        "version": version,
        "system_label": system_label,
        "question_count": len(rows),
        "numeric_o": [row.numeric_accuracy for row in rows].count("O"),
        "numeric_x": [row.numeric_accuracy for row in rows].count("X"),
        "numeric_na": [row.numeric_accuracy for row in rows].count("NA"),
        "qualitative_avg": round(sum(row.qualitative_score for row in rows) / len(rows), 2),
        "by_category": by_category,
    }


def _write_summary_md(path: Path, rows: list[ScoredRow], summary: dict[str, JsonValue]) -> None:
    lines = [
        "# Evaluation Summary",
        "",
        f"- system_label: `{summary['system_label']}`",
        f"- version: `{summary['version']}`",
        f"- question_count: {summary['question_count']}",
        f"- numeric: O={summary['numeric_o']} / X={summary['numeric_x']} / NA={summary['numeric_na']}",
        f"- qualitative_avg: {summary['qualitative_avg']}",
        "",
        "## Weak Spots",
    ]
    weak_rows = [
        row
        for row in rows
        if row.numeric_accuracy == "X" or row.qualitative_score <= 3
    ][:20]
    for row in weak_rows:
        lines.append(
            f"- {row.question.question_id} ({row.question.category}): "
            f"numeric={row.numeric_accuracy}, qualitative={row.qualitative_score}, note={row.note}"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _load_previous_rows(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return {}
    previous: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            row_id = row.get("id")
            if isinstance(row_id, str):
                previous[row_id] = row
    return previous


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    questions = load_questions(args.questions, args.pl_questions)
    raw_results = load_raw_results(args.raw_results_jsonl)
    rows = [score_question(question, raw_results.get(question.question_id)) for question in questions]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = args.output_dir / f"stage0_evalset_{args.version}.xlsx"
    baseline_path = args.output_dir / f"baseline_{args.version}.json"
    gold_path = args.output_dir / f"gold_{args.version}.json"
    summary_path = args.output_dir / f"summary_{args.version}.md"
    previous_rows = _load_previous_rows(args.previous_baseline_json)
    previous_blocks = ((args.previous_version, previous_rows),) if previous_rows else ()
    write_workbook(xlsx_path, rows, version=args.version, previous_blocks=previous_blocks)
    write_json(baseline_path, {"rows": [_row_to_json(row) for row in rows]})
    write_json(
        gold_path,
        {
            row.question.question_id: {
                "gold_note": row.question.gold_note,
                "observations": _row_to_json(row)["gold_observations"],
            }
            for row in rows
        },
    )
    summary = _summary_payload(rows, args.version, args.system_label)
    write_json(args.output_dir / f"summary_{args.version}.json", summary)
    _write_summary_md(summary_path, rows, summary)
    print(xlsx_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
