from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import time

from scripts.f21_probe.artifacts import (
    atomic_json,
    flush_progress,
    prepare_output,
    utc_now,
    write_captured_scenario,
    write_run_metadata,
    write_skipped_scenario,
)
from scripts.f21_probe.capture import capture_turn, cleanup_sessions, stream_endpoint
from scripts.f21_probe.models import QuestionSet, QuestionSetCounts, question_set_counts
from scripts.f21_probe.planning import artifact_path, conversation_plans, formatted_case_id
from scripts.f21_probe.schema import SUMMARY_SCHEMA
from scripts.f21_probe.sse import JsonObject
from scripts.f21_probe.types import ConversationPlan, OutputRow, RunOptions


class RequestPacer:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._next_start = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_start - now)
            if delay:
                time.sleep(delay)
            self._next_start = time.monotonic() + self._interval_seconds


def run_probe(question_set: QuestionSet, options: RunOptions) -> int:
    prepare_output(options.output)
    started = time.perf_counter()
    started_utc = utc_now()
    plans = conversation_plans(question_set)
    counts = question_set_counts(question_set)
    pacer = RequestPacer(options.interval_seconds)
    rows: dict[tuple[int, int], OutputRow] = {}
    rows_lock = Lock()
    sessions = [plan.session_id for plan in plans if not plan.scenario.skip_reason]

    write_run_metadata(
        options,
        started_utc=started_utc,
        finished_utc=None,
        status="RUNNING",
    )
    for plan in plans:
        if plan.scenario.skip_reason:
            write_skipped_scenario(options.output, plan)

    def capture_plan(plan: ConversationPlan) -> None:
        if plan.scenario.skip_reason:
            return
        scenario_rows: list[OutputRow] = []
        for turn_number, turn in enumerate(plan.scenario.turns, start=1):
            pacer.wait()
            row = capture_turn(
                root=options.output,
                relative=artifact_path(plan, turn.case_id, turn_number),
                endpoint=stream_endpoint(options.base_url, options.stream_path),
                headers=options.headers,
                timeout_seconds=options.request_timeout_seconds,
                stage=plan.stage.id,
                case_id=formatted_case_id(turn.case_id, plan, turn_number),
                question=turn.question,
                session_id=plan.session_id,
                repetition=plan.repetition if plan.stage.multiturn_sets else None,
                turn=turn_number,
            )
            scenario_rows.append(row)
            with rows_lock:
                rows[(plan.order, turn_number)] = row
                flush_progress(options.output, rows)
        if plan.stage.multiturn_sets:
            write_captured_scenario(options.output, plan, scenario_rows)

    with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
        futures = [executor.submit(capture_plan, plan) for plan in plans]
        for future in futures:
            future.result()

    ordered_rows = [rows[key] for key in sorted(rows)]
    cleanup = cleanup_sessions(options, sessions)
    summary = _capture_summary(
        ordered_rows,
        counts=counts,
        cleanup=cleanup,
        elapsed_seconds=time.perf_counter() - started,
    )
    atomic_json(options.output / "capture_summary.json", summary)
    write_run_metadata(
        options,
        started_utc=started_utc,
        finished_utc=utc_now(),
        status="COMPLETE",
    )
    return 0


def _capture_summary(
    rows: list[OutputRow],
    *,
    counts: QuestionSetCounts,
    cleanup: JsonObject,
    elapsed_seconds: float,
) -> JsonObject:
    dispositions = Counter(str(row.get("disposition") or "missing") for row in rows)
    return {
        "schema": SUMMARY_SCHEMA,
        "expected_question_answer_pairs": counts.question_answer_pairs,
        "captured_question_answer_pairs": len(rows),
        "expected_multiturn_sets": counts.multiturn_sets,
        "executed_multiturn_sets": (
            counts.multiturn_sets - counts.skipped_multiturn_sets
        ),
        "skipped_multiturn_sets": counts.skipped_multiturn_sets,
        "stage_counts": dict(Counter(str(row["stage"]) for row in rows)),
        "disposition_counts": dict(dispositions),
        "http_or_capture_error_count": sum(bool(row["error"]) for row in rows),
        "total_client_elapsed_s": round(elapsed_seconds, 3),
        "cleanup": cleanup,
        "rows": [_summary_row(row) for row in rows],
    }


def _summary_row(row: OutputRow) -> JsonObject:
    keys = (
        "stage",
        "case_id",
        "question",
        "pod",
        "trace_id",
        "disposition",
        "tools_called",
        "total_elapsed_ms",
        "client_elapsed_s",
        "http_status",
        "error",
    )
    return {key: row[key] for key in keys}
