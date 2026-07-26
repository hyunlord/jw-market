from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import time

from scripts.f21_probe.planning import scenario_directory
from scripts.f21_probe.schema import RUN_METADATA_SCHEMA
from scripts.f21_probe.sse import JsonObject
from scripts.f21_probe.types import ConversationPlan, OutputRow, RunOptions


def prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def flush_progress(root: Path, rows: dict[tuple[int, int], OutputRow]) -> None:
    ordered = [rows[key] for key in sorted(rows)]
    atomic_json(
        root / "progress.json",
        {
            "captured_rows": len(ordered),
            "last_case": ordered[-1]["case_id"] if ordered else None,
            "http_error_rows": sum(bool(row["error"]) for row in ordered),
            "updated_utc": utc_now(),
        },
    )


def write_run_metadata(
    options: RunOptions,
    *,
    started_utc: str,
    finished_utc: str | None,
    status: str,
) -> None:
    question_set_paths = options.question_set_paths or (options.question_set_path,)
    question_sets = [
        {
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in question_set_paths
    ]
    atomic_json(
        options.output / "run_metadata.json",
        {
            "schema": RUN_METADATA_SCHEMA,
            "status": status,
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "target": {
                "commit": options.target.commit,
                "generation": options.target.generation,
                "digest": options.target.digest,
                "base_url": options.base_url,
                "stream_path": options.stream_path,
            },
            "question_set": {
                "path": str(options.question_set_path),
                "sha256": sha256(options.question_set_path.read_bytes()).hexdigest(),
            },
            "question_sets": question_sets,
            "execution": {
                "concurrency": options.concurrency,
                "interval_seconds": options.interval_seconds,
                "request_timeout_seconds": options.request_timeout_seconds,
                "cleanup_url": options.cleanup_url,
                "header_environment": options.header_sources,
            },
        },
    )


def write_skipped_scenario(root: Path, plan: ConversationPlan) -> None:
    directory = root / scenario_directory(plan)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(
        directory / "scenario.json",
        {
            "scenario": plan.scenario.id,
            "repetition": plan.repetition,
            "status": "SKIP",
            "reason": plan.scenario.skip_reason,
            "requires": plan.scenario.requires,
            "turns": [],
        },
    )


def write_captured_scenario(
    root: Path,
    plan: ConversationPlan,
    rows: list[OutputRow],
) -> None:
    directory = root / scenario_directory(plan)
    atomic_json(
        directory / "scenario.json",
        {
            "scenario": plan.scenario.id,
            "repetition": plan.repetition,
            "status": "CAPTURED",
            "conversation_id": plan.session_id,
            "turns": [_scenario_turn(row) for row in rows],
        },
    )


def _scenario_turn(row: OutputRow) -> JsonObject:
    sse_file = str(row["sse_file"])
    return {
        "case_id": row["case_id"],
        "question": row["question"],
        "pod": row["pod"],
        "trace_id": row["trace_id"],
        "disposition": row["disposition"],
        "json_file": str(PurePosixPath(sse_file).with_suffix(".json")),
        "sse_file": sse_file,
    }


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
