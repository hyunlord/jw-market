#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pymysql",
# ]
# ///
"""Plan or execute occurrence-preserving semantic row-topic assignment waves."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymysql

from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (
    SCHEMA,
    connect_mariadb,
    read_env_file,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_assignment import (
    AssignmentInputRow,
    AssignmentParseError,
    RowTopicAssignment,
    TopicRubric,
    parse_assignment_response_allow_missing,
    row_topic_prompt,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_db import PreparedRun, prepare_run
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_execute import (
    PROMPT_VERSION,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_runner import (
    AssignmentBatch,
    AssignmentChatClient,
    EMPIRICAL_USD_PER_CALL,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_db import (
    finish_semantic_run,
    load_bridge_occurrences,
    load_semantic_batch_calls,
    load_semantic_batch_statuses,
    start_semantic_run,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_execute import (
    SemanticBatchOutcome,
    SemanticClassification,
    execute_semantic_batch,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_semantic_runner import (
    OccurrenceResult,
    SemanticBatch,
    SemanticOccurrence,
    build_semantic_batches,
    rewave_semantic_batch,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import validated_stage_schema


MAX_WAVE_CALLS = 350
DEFAULT_BATCH_SIZE = 150
FAILED_RESPONSE_LIMIT_BYTES = 65_536
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|password|secret|authorization)[\"']?\s*[:=]\s*[\"'])[^\"']+([\"'])"
)


class SemanticAdapterError(RuntimeError):
    """Preserve paid-attempt accounting when an adapter batch fails."""

    def __init__(
        self,
        message: str,
        *,
        calls_used: int = 0,
        raw_responses: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.calls_used = calls_used
        self.raw_responses = raw_responses


class SemanticLlmCallError(SemanticAdapterError):
    """Raised when the reused GenOS serving path fails before parsing."""


class SemanticLlmTimeoutError(SemanticLlmCallError):
    """Raised when the reused GenOS serving path times out."""


class SemanticResponseParseError(SemanticAdapterError):
    """Raised when GenOS returned a response that cannot be mapped safely."""


@dataclass(frozen=True, slots=True)
class TopicOccurrenceSet:
    topic_set_version: str
    occurrences: tuple[SemanticOccurrence, ...]


@dataclass(frozen=True, slots=True)
class PlannedSemanticBatch:
    topic_set_version: str
    batch: SemanticBatch


@dataclass(frozen=True, slots=True)
class SemanticWave:
    wave_no: int
    batches: tuple[PlannedSemanticBatch, ...]


@dataclass(frozen=True, slots=True)
class SemanticWavePlan:
    waves: tuple[SemanticWave, ...]
    total_occurrences: int
    total_calls: int
    estimated_usd: float


@dataclass(slots=True)
class CallBudget:
    maximum: int
    used: int = 0


@dataclass(frozen=True, slots=True)
class WaveExecutionSummary:
    wave_no: int
    completed_batches: int
    skipped_batches: int
    failed_batches: int
    calls_used: int
    assignment_rows: int
    status_rows: int
    dropped_unexpected_count: int
    dropped_missing_count: int


class BatchOutcome(Protocol):
    status: str
    calls_used: int
    assignment_rows: int
    status_rows: int
    dropped_unexpected_count: int
    dropped_missing_count: int


class AssignmentClient(Protocol):
    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]: ...


def _classify_semantic_lenient_row_ids(
    client: AssignmentClient | None,
    rubric: tuple[TopicRubric, ...],
    rows: tuple[AssignmentInputRow, ...],
    topic_set_version: str,
    batch_id: str,
    *,
    max_calls: int,
    calls_used: int,
) -> dict[str, object]:
    """Retry malformed responses, but retain valid rows around row-id drift."""
    if client is None:
        raise AssignmentParseError("semantic GenOS client is not configured")
    known_topic_ids = {topic.topic_id for topic in rubric}
    last_error = ""
    for attempt in (1, 2):
        if max_calls and calls_used + attempt > max_calls:
            raise AssignmentParseError(f"call cap {max_calls} reached before {batch_id}")
        content, _usage, _latency_ms = client.classify(row_topic_prompt(rubric, rows))
        try:
            parsed = parse_assignment_response_allow_missing(
                content,
                list(rows),
                known_topic_ids,
                topic_set_version,
                batch_id,
                allow_unexpected=True,
            )
            return {
                "assignments": parsed.assignments,
                "calls": attempt,
                "missing_row_ids": list(parsed.missing_row_ids),
                "unexpected_row_ids": list(parsed.unexpected_row_ids),
            }
        except AssignmentParseError as exc:
            last_error = str(exc)
            if attempt == 2:
                raise
    raise AssignmentParseError(last_error)


class RecordingAssignmentClient:
    """Capture GenOS response content without retaining prompts or credentials."""

    def __init__(self, client: AssignmentClient) -> None:
        self._client = client
        self._responses: list[str] = []

    @property
    def responses(self) -> tuple[str, ...]:
        return tuple(self._responses)

    def clear(self) -> None:
        self._responses.clear()

    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
        content, usage, latency_ms = self._client.classify(messages)
        self._responses.append(content)
        return content, usage, latency_ms


class BudgetedAssignmentClient:
    """Count every paid attempt, including timeout and parse-retry paths."""

    def __init__(self, client: AssignmentChatClient, budget: CallBudget) -> None:
        self._client = client
        self._budget = budget

    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
        if self._budget.used >= self._budget.maximum:
            raise AssignmentParseError(
                f"call cap {self._budget.maximum} reached before serving request"
            )
        self._budget.used += 1
        return self._client.classify(messages)


LegacyClassifier = Callable[..., dict[str, object]]


class LegacyGenosSemanticAdapter:
    """Map semantic occurrences through the existing row-topic GenOS path."""

    def __init__(
        self,
        *,
        prepared: PreparedRun,
        client: AssignmentClient | None,
        call_budget: CallBudget,
        response_recorder: RecordingAssignmentClient | None = None,
        classify_legacy: LegacyClassifier = _classify_semantic_lenient_row_ids,
    ) -> None:
        self._prepared = prepared
        self._client = client
        self._call_budget = call_budget
        self._response_recorder = response_recorder
        self._classify_legacy = classify_legacy
        self._rows = {(row.scope_id, row.row_id): row for row in prepared.rows}

    @classmethod
    def from_test_rows(
        cls,
        *,
        topic_set_version: str,
        rows: tuple[AssignmentInputRow, ...],
        rubrics: dict[tuple[str, str], tuple[TopicRubric, ...]],
        classify_legacy: LegacyClassifier,
    ) -> LegacyGenosSemanticAdapter:
        return cls(
            prepared=PreparedRun(topic_set_version=topic_set_version, rows=rows, rubrics=rubrics),
            client=None,
            call_budget=CallBudget(MAX_WAVE_CALLS),
            classify_legacy=classify_legacy,
        )

    def classify(self, batch: SemanticBatch) -> SemanticClassification:
        calls_before = self._call_budget.used
        if self._response_recorder is not None:
            self._response_recorder.clear()
        rows = tuple(self._row_for(item) for item in batch.occurrences)
        rubric = self._prepared.rubrics.get((batch.scope_id, batch.brand))
        if rubric is None:
            raise SemanticResponseParseError(
                f"missing rubric for {batch.scope_id} / {batch.brand}"
            )
        legacy_batch = AssignmentBatch(batch_id=batch.batch_id, rows=rows)
        try:
            parsed = self._classify_legacy(
                self._client,
                rubric,
                legacy_batch.rows,
                self._prepared.topic_set_version,
                legacy_batch.batch_id,
                max_calls=self._call_budget.maximum,
                calls_used=self._call_budget.used,
            )
        except AssignmentParseError as exc:
            message = str(exc)
            calls_used = self._call_budget.used - calls_before
            raw_responses = (
                self._response_recorder.responses if self._response_recorder is not None else ()
            )
            if message.startswith("call cap"):
                raise SemanticLlmCallError(
                    message, calls_used=calls_used, raw_responses=raw_responses
                ) from exc
            if message.startswith("serving call failed"):
                if "TimeoutError" in message:
                    raise SemanticLlmTimeoutError(
                        message, calls_used=calls_used, raw_responses=raw_responses
                    ) from exc
                raise SemanticLlmCallError(
                    message, calls_used=calls_used, raw_responses=raw_responses
                ) from exc
            raise SemanticResponseParseError(
                message, calls_used=calls_used, raw_responses=raw_responses
            ) from exc
        reported_calls = int(parsed.get("calls", 0))
        if self._client is None:
            self._call_budget.used += reported_calls
        calls_used = self._call_budget.used - calls_before
        if calls_used <= 0:
            raise SemanticResponseParseError("legacy adapter returned no call count")
        if reported_calls != calls_used:
            raise SemanticResponseParseError(
                f"legacy call accounting mismatch: reported={reported_calls}, observed={calls_used}"
            )
        assignments = parsed.get("assignments")
        if not isinstance(assignments, list) or not all(
            isinstance(item, RowTopicAssignment) for item in assignments
        ):
            raise SemanticResponseParseError("legacy adapter returned invalid assignments")
        missing_row_ids = tuple(sorted({int(value) for value in parsed.get("missing_row_ids", [])}))
        unexpected_row_ids = {int(value) for value in parsed.get("unexpected_row_ids", [])}
        topics_by_row: dict[int, set[str]] = {
            occurrence.stage_row_id: set() for occurrence in batch.occurrences
        }
        raw_responses = (
            self._response_recorder.responses if self._response_recorder is not None else ()
        )
        for assignment in assignments:
            if assignment.row_id not in topics_by_row:
                unexpected_row_ids.add(assignment.row_id)
                continue
            topics_by_row[assignment.row_id].add(assignment.topic_id)
        unexpected = tuple(sorted(unexpected_row_ids))
        missing = tuple(row_id for row_id in missing_row_ids if row_id in topics_by_row)
        if unexpected or missing:
            print(
                json.dumps(
                    {
                        "event": "semantic_row_id_dropped",
                        "batch_id": batch.batch_id,
                        "unexpected_count": len(unexpected),
                        "unexpected_row_ids": unexpected,
                        "missing_count": len(missing),
                        "missing_row_ids": missing,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        return SemanticClassification(
            results=tuple(
                OccurrenceResult(stage_row_id=row_id, topic_ids=tuple(sorted(topic_ids)))
                for row_id, topic_ids in sorted(topics_by_row.items())
            ),
            calls_used=calls_used,
            raw_responses=raw_responses,
            dropped_unexpected_row_ids=unexpected,
            dropped_missing_row_ids=missing,
        )

    def _row_for(self, occurrence: SemanticOccurrence) -> AssignmentInputRow:
        row = self._rows.get((occurrence.scope_id, occurrence.stage_row_id))
        if row is None:
            raise SemanticResponseParseError(
                "bridge occurrence has no matching legacy assignment input: "
                f"scope={occurrence.scope_id}, stage_row_id={occurrence.stage_row_id}"
            )
        if row.brand != occurrence.brand:
            raise SemanticResponseParseError(
                "bridge occurrence brand differs from legacy assignment input"
            )
        return row


def build_wave_plan(
    inputs: tuple[TopicOccurrenceSet, ...],
    *,
    prompt_version: str,
    batch_size: int,
    max_calls: int,
) -> SemanticWavePlan:
    """Create stable global waves without changing scope/brand batch boundaries."""
    _validate_max_calls(max_calls)
    provisional: list[PlannedSemanticBatch] = []
    for item in inputs:
        batches = build_semantic_batches(
            item.occurrences,
            topic_set_version=item.topic_set_version,
            prompt_version=prompt_version,
            wave_no=0,
            batch_size=batch_size,
        )
        provisional.extend(PlannedSemanticBatch(item.topic_set_version, batch) for batch in batches)
    waves: list[SemanticWave] = []
    for offset in range(0, len(provisional), max_calls):
        wave_no = len(waves) + 1
        selected = provisional[offset : offset + max_calls]
        waves.append(
            SemanticWave(
                wave_no=wave_no,
                batches=tuple(
                    PlannedSemanticBatch(
                        topic_set_version=item.topic_set_version,
                        batch=rewave_semantic_batch(
                            item.batch,
                            topic_set_version=item.topic_set_version,
                            prompt_version=prompt_version,
                            wave_no=wave_no,
                        ),
                    )
                    for item in selected
                ),
            )
        )
    total_calls = len(provisional)
    return SemanticWavePlan(
        waves=tuple(waves),
        total_occurrences=sum(len(item.occurrences) for item in inputs),
        total_calls=total_calls,
        estimated_usd=total_calls * EMPIRICAL_USD_PER_CALL,
    )


def execute_wave(
    wave: SemanticWave,
    *,
    execute_batch: Callable[[PlannedSemanticBatch], BatchOutcome],
    stop_on_response_parse: bool = False,
) -> WaveExecutionSummary:
    """Execute one explicit wave, continuing only past recorded quality failures."""
    completed = skipped = failed = calls = assignment_rows = status_rows = 0
    dropped_unexpected = dropped_missing = 0
    for item in wave.batches:
        try:
            outcome = execute_batch(item)
        except SemanticResponseParseError as exc:
            failed += 1
            calls += exc.calls_used
            print(
                json.dumps(
                    {
                        "event": "batch_failed",
                        "failure_kind": "response_parse",
                        "batch_id": item.batch.batch_id,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            if stop_on_response_parse:
                raise
            continue
        calls += outcome.calls_used
        assignment_rows += outcome.assignment_rows
        status_rows += outcome.status_rows
        dropped_unexpected += int(getattr(outcome, "dropped_unexpected_count", 0))
        dropped_missing += int(getattr(outcome, "dropped_missing_count", 0))
        if outcome.calls_used == 0:
            skipped += 1
        else:
            completed += 1
        print(
            json.dumps(
                {
                    "event": "batch_complete",
                    "batch_id": item.batch.batch_id,
                    "calls_used": outcome.calls_used,
                    "cumulative_calls": calls,
                    "cumulative_usd": round(calls * EMPIRICAL_USD_PER_CALL, 6),
                    "dropped_unexpected": int(
                        getattr(outcome, "dropped_unexpected_count", 0)
                    ),
                    "dropped_missing": int(getattr(outcome, "dropped_missing_count", 0)),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
    return WaveExecutionSummary(
        wave_no=wave.wave_no,
        completed_batches=completed,
        skipped_batches=skipped,
        failed_batches=failed,
        calls_used=calls,
        assignment_rows=assignment_rows,
        status_rows=status_rows,
        dropped_unexpected_count=dropped_unexpected,
        dropped_missing_count=dropped_missing,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=SCHEMA)
    parser.add_argument("--stage-generation-id", required=True)
    parser.add_argument("--topic-set-version", action="append", required=True)
    parser.add_argument("--wave-no", type=int, required=True)
    parser.add_argument("--max-calls", type=_max_calls_argument, default=MAX_WAVE_CALLS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--prompt-version", default=PROMPT_VERSION)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--release-id", default="")
    parser.add_argument("--created-by", default="codex")
    parser.add_argument(
        "--stop-on-response-parse",
        action="store_true",
        help="stop the wave after recording the first response parse failure",
    )
    parser.add_argument("--base-url", default="https://jwai-dev.jwhealthcare.com")
    parser.add_argument("--serving-id", default="163")
    parser.add_argument(
        "--failed-response-log",
        type=Path,
        default=Path(
            os.environ.get(
                "ROW_TOPIC_FAILED_RESPONSE_LOG",
                "/tmp/row_topic_semantic_failed_responses.jsonl",
            )
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.wave_no <= 0:
        parser.error("--wave-no must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.execute and (not args.run_id or not args.release_id):
        parser.error("--execute requires --run-id and --release-id")
    args.dry_run = not args.execute
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    schema = validated_stage_schema(args.schema)
    connection = _connect_semantic_db()
    try:
        inputs, prepared = _load_inputs(
            connection,
            schema=schema,
            stage_generation_id=args.stage_generation_id,
            topic_set_versions=tuple(args.topic_set_version),
            scope_filter=tuple(args.scope),
        )
        plan = build_wave_plan(
            inputs,
            prompt_version=args.prompt_version,
            batch_size=args.batch_size,
            max_calls=args.max_calls,
        )
        selected = _selected_wave(plan, args.wave_no)
        if args.dry_run:
            _print_json(_dry_run_payload(args, plan, selected))
            return 0
        return _execute_selected_wave(connection, args=args, schema=schema, plan=plan, wave=selected, prepared=prepared)
    finally:
        connection.close()


def _load_inputs(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    stage_generation_id: str,
    topic_set_versions: tuple[str, ...],
    scope_filter: tuple[str, ...],
) -> tuple[tuple[TopicOccurrenceSet, ...], dict[str, PreparedRun]]:
    inputs: list[TopicOccurrenceSet] = []
    prepared_by_version: dict[str, PreparedRun] = {}
    for version in topic_set_versions:
        prepared = prepare_run(connection, schema=schema, topic_set_version=version)
        scopes = tuple(sorted({scope for scope, _brand in prepared.rubrics}))
        selected_scopes = tuple(scope for scope in scopes if not scope_filter or scope in scope_filter)
        occurrences = load_bridge_occurrences(
            connection,
            schema=schema,
            stage_generation_id=stage_generation_id,
            topic_set_version=version,
            scope_ids=selected_scopes,
        )
        if occurrences and {item.stage_generation_id for item in occurrences} != {stage_generation_id}:
            raise RuntimeError("bridge query returned an unexpected stage generation")
        inputs.append(TopicOccurrenceSet(version, occurrences))
        prepared_by_version[version] = prepared
    return tuple(inputs), prepared_by_version


def _connect_semantic_db() -> pymysql.connections.Connection:
    """Prefer the existing row-topic writer account in runtime containers."""
    row_topic_password = os.environ.get("ROW_TOPIC_DB_PASSWORD", "")
    if not row_topic_password:
        return connect_mariadb(read_env_file())
    return pymysql.connect(
        host=os.environ.get("ROW_TOPIC_DB_HOST", os.environ.get("MARIADB_HOST", "127.0.0.1")),
        port=int(os.environ.get("ROW_TOPIC_DB_PORT", os.environ.get("MARIADB_PORT", "3306"))),
        user=os.environ.get("ROW_TOPIC_DB_USER", os.environ.get("MARIADB_USER", "")),
        password=row_topic_password,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _execute_selected_wave(
    connection: pymysql.connections.Connection,
    *,
    args: argparse.Namespace,
    schema: str,
    plan: SemanticWavePlan,
    wave: SemanticWave,
    prepared: dict[str, PreparedRun],
) -> int:
    now = _utc_naive()
    should_execute = start_semantic_run(
        connection,
        schema=schema,
        run_id=args.run_id,
        release_id=args.release_id,
        stage_generation_id=args.stage_generation_id,
        prompt_version=args.prompt_version,
        execution_mode="wave",
        planned_occurrences=plan.total_occurrences,
        planned_calls=plan.total_calls,
        started_at_utc_naive=now,
        created_by=args.created_by,
    )
    if not should_execute:
        _print_json({"mode": "execute", "status": "complete_noop", "calls_used": 0})
        return 0
    token = os.environ.get("GENOS_BEARER_TOKEN", "")
    if not token:
        raise RuntimeError("GENOS_BEARER_TOKEN is not set")
    budget = CallBudget(args.max_calls)
    raw_client = AssignmentChatClient(base_url=args.base_url, token=token, serving_id=args.serving_id)
    budgeted_client = BudgetedAssignmentClient(raw_client, budget)
    client = RecordingAssignmentClient(budgeted_client)
    adapters = {
        version: LegacyGenosSemanticAdapter(
            prepared=item,
            client=client,
            call_budget=budget,
            response_recorder=client,
        )
        for version, item in prepared.items()
    }
    started = time.monotonic()

    def run_batch(item: PlannedSemanticBatch) -> SemanticBatchOutcome:
        outcome = execute_semantic_batch(
            connection,
            schema=schema,
            run_id=args.run_id,
            batch=item.batch,
            topic_set_version=item.topic_set_version,
            prompt_version=args.prompt_version,
            classified_at_utc_naive=_utc_naive(),
            classify=adapters[item.topic_set_version].classify,
            preserve_failed_response=lambda record: append_failed_response_log(
                args.failed_response_log,
                run_id=record.run_id,
                batch_id=record.batch_id,
                error_code=record.error_code,
                responses=record.responses,
                recorded_at_utc_naive=record.recorded_at_utc_naive,
            ),
        )
        print(
            json.dumps(
                {"event": "elapsed", "seconds": round(time.monotonic() - started, 3)},
                sort_keys=True,
            ),
            flush=True,
        )
        return outcome

    summary = execute_wave(
        wave,
        execute_batch=run_batch,
        stop_on_response_parse=args.stop_on_response_parse,
    )
    all_batch_ids = tuple(item.batch.batch_id for planned_wave in plan.waves for item in planned_wave.batches)
    states = load_semantic_batch_statuses(
        connection,
        schema=schema,
        run_id=args.run_id,
        batch_ids=all_batch_ids,
    )
    if wave.wave_no == len(plan.waves) and len(states) == len(all_batch_ids) and all(
        status == "complete" for status in states.values()
    ):
        finish_semantic_run(
            connection,
            schema=schema,
            run_id=args.run_id,
            calls_used=load_semantic_batch_calls(
                connection,
                schema=schema,
                run_id=args.run_id,
                batch_ids=all_batch_ids,
            ),
            failed_batches=0,
            finished_at_utc_naive=_utc_naive(),
        )
    _print_json(
        {
            "mode": "execute",
            "wave_no": summary.wave_no,
            "completed_batches": summary.completed_batches,
            "skipped_batches": summary.skipped_batches,
            "failed_batches": summary.failed_batches,
            "calls_used": summary.calls_used,
            "assignment_rows": summary.assignment_rows,
            "status_rows": summary.status_rows,
            "dropped_unexpected_total": summary.dropped_unexpected_count,
            "dropped_missing_total": summary.dropped_missing_count,
            "run_terminal": len(states) == len(all_batch_ids)
            and all(status == "complete" for status in states.values()),
        }
    )
    return 0 if summary.failed_batches == 0 else 2


def _dry_run_payload(
    args: argparse.Namespace,
    plan: SemanticWavePlan,
    selected: SemanticWave,
) -> dict[str, object]:
    return {
        "mode": "dry-run",
        "skipped_execution": True,
        "actual_llm_calls": 0,
        "stage_generation_id": args.stage_generation_id,
        "topic_set_versions": args.topic_set_version,
        "batch_size": args.batch_size,
        "max_calls": args.max_calls,
        "total_occurrences": plan.total_occurrences,
        "total_calls": plan.total_calls,
        "estimated_usd": round(plan.estimated_usd, 6),
        "waves": [
            {"wave_no": wave.wave_no, "calls": len(wave.batches)} for wave in plan.waves
        ],
        "selected_wave": selected.wave_no,
        "selected_calls": len(selected.batches),
    }


def append_failed_response_log(
    path: Path,
    *,
    run_id: str,
    batch_id: str,
    error_code: str,
    responses: tuple[str, ...],
    recorded_at_utc_naive: str,
) -> None:
    """Append sanitized failed-call responses without prompts or credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    payload = {
        "run_id": run_id,
        "batch_id": batch_id,
        "error_code": error_code,
        "recorded_at_utc_naive": recorded_at_utc_naive,
        "responses": [_sanitized_response(response) for response in responses],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _sanitized_response(response: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", response)
    redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
    redacted = _SECRET_FIELD_PATTERN.sub(r"\1[REDACTED]\2", redacted)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= FAILED_RESPONSE_LIMIT_BYTES:
        return redacted
    return encoded[:FAILED_RESPONSE_LIMIT_BYTES].decode("utf-8", errors="ignore") + "[TRUNCATED]"


def _selected_wave(plan: SemanticWavePlan, wave_no: int) -> SemanticWave:
    for wave in plan.waves:
        if wave.wave_no == wave_no:
            return wave
    raise RuntimeError(f"wave {wave_no} does not exist; planned_waves={len(plan.waves)}")


def _validate_max_calls(value: int) -> None:
    if value <= 0 or value > MAX_WAVE_CALLS:
        raise ValueError(f"max_calls must be between 1 and {MAX_WAVE_CALLS}")


def _max_calls_argument(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > MAX_WAVE_CALLS:
        raise argparse.ArgumentTypeError(
            f"max_calls must be between 1 and {MAX_WAVE_CALLS}; received {parsed}"
        )
    return parsed


def _utc_naive() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
