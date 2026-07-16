"""Executes a plan: subprocess per builder command, JSON event log, checkpoints.

Log events are one JSON object per line so a later Temporal migration can map
each stage to an Activity boundary without changing the builders.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, IO

from pipeline.orchestrator.planner import Plan, StagePlan
from pipeline.orchestrator.stages import Command
from pipeline.orchestrator.state import StateStore

Runner = Callable[[Command], int]


def _default_runner(command: Command) -> int:
    return subprocess.run(list(command.argv), check=False).returncode


class EventLog:
    def __init__(self, run_id: str, stream: IO[str] | None = None, log_file: Path | None = None) -> None:
        self.run_id = run_id
        self.stream = stream if stream is not None else sys.stdout
        self.log_file = log_file
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **detail) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": event,
            **detail,
        }
        line = json.dumps(record, ensure_ascii=False)
        print(line, file=self.stream, flush=True)
        if self.log_file is not None:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def execute_plan(
    plan: Plan,
    state: StateStore,
    log: EventLog,
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
) -> int:
    """Returns a process exit code. Aborts the chain on the first stage failure."""

    runner = runner or _default_runner
    log.emit("plan", mode=plan.mode, epoch=plan.epoch, warnings=plan.warnings, stages=plan.to_json()["stages"])

    if dry_run:
        log.emit("dry_run_end", note="no command executed, no state written, write 0")
        return 0

    if plan.blocked:
        for stage in plan.blocked:
            log.emit("blocked", stage=stage.key, reason=stage.reason)
        return 2

    if plan.epoch is None:
        log.emit("abort", reason="mart epoch unknown; refusing to execute (fail-closed)")
        return 2

    for stage in plan.stages:
        if stage.action != "run":
            log.emit("stage_skip", stage=stage.key, action=stage.action, reason=stage.reason)
            continue
        exit_code = _run_stage(stage, plan, state, log, runner)
        if exit_code != 0:
            log.emit("chain_abort", stage=stage.key, reason="stage failed; downstream stages not started")
            return exit_code
    log.emit("run_end", status="completed")
    return 0


def _run_stage(stage: StagePlan, plan: Plan, state: StateStore, log: EventLog, runner: Runner) -> int:
    log.emit(
        "stage_start",
        stage=stage.key,
        reason=stage.reason,
        forced=stage.forced,
        scope_brands=list(stage.scope_brands),
        commands=len(stage.commands),
    )
    started = time.monotonic()
    for index, command in enumerate(stage.commands):
        log.emit("command_start", stage=stage.key, index=index, purpose=command.purpose, argv=list(command.argv))
        exit_code = runner(command)
        log.emit("command_end", stage=stage.key, index=index, exit_code=exit_code)
        if exit_code != 0:
            state.record(stage.key, status="failed", epoch=plan.epoch or "", forced=stage.forced)
            log.emit("stage_end", stage=stage.key, status="failed", duration_s=round(time.monotonic() - started, 1))
            return exit_code
    state.record(stage.key, status="completed", epoch=plan.epoch or "", forced=stage.forced)
    log.emit("stage_end", stage=stage.key, status="completed", duration_s=round(time.monotonic() - started, 1))
    return 0
