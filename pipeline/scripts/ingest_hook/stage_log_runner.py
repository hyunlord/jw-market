"""Run an ingest Job while teeing stdout/stderr into durable per-stage logs."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from pipeline.scripts.ingest_hook import config, db_credential_preflight, stage_logs

_STAGE_MARKER = re.compile(
    r"^\[stage\]\s+([a-z0-9_]+)\s+(?:start|end|skipped)\b"
)
_RUNNER_MODULES = {
    "ingest": "pipeline.scripts.ingest_hook.job_runner",
    "complete-reingest": "pipeline.scripts.ingest_hook.complete_reingest_runner",
}


def run(
    *,
    manifest: Path,
    run_id: str,
    job_name: str,
    runner: str = "ingest",
    runner_args: tuple[str, ...] = (),
) -> int:
    root = config.log_root()
    full_path = stage_logs.full_log_path(root, job_name=job_name)
    stage_logs.ensure_log_file(full_path)
    try:
        db_credential_preflight.run_preflight()
    except db_credential_preflight.DBCredentialPreflightError as exc:
        line = stage_logs.redact(
            f"preflight=db_credentials status=fail reason={exc}\n"
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        with full_path.open("a", encoding="utf-8") as full:
            full.write(line)
            full.flush()
        return 2
    else:
        line = "preflight=db_credentials status=pass query=SELECT_1\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        with full_path.open("a", encoding="utf-8") as full:
            full.write(line)
            full.flush()

    try:
        runner_module = _RUNNER_MODULES[runner]
    except KeyError as exc:
        raise ValueError(f"unsupported stage-log runner: {runner!r}") from exc
    effective_args = runner_args or (
        "--manifest",
        str(manifest),
        "--run-id",
        run_id,
    )
    command = [sys.executable, "-m", runner_module, *effective_args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    assert process.stdout is not None
    current_stage: str | None = None
    with full_path.open("a", encoding="utf-8") as full:
        for line in process.stdout:
            durable_line = stage_logs.redact(line)
            sys.stdout.write(durable_line)
            sys.stdout.flush()
            full.write(durable_line)
            full.flush()

            marker = _STAGE_MARKER.match(line)
            marker_stage = marker.group(1) if marker else None
            if marker_stage and " start" in line:
                current_stage = marker_stage
            target_stage = marker_stage or current_stage
            if target_stage:
                path = stage_logs.stage_log_path(
                    root, job_name=job_name, stage=target_stage
                )
                stage_logs.ensure_log_file(path)
                with path.open("a", encoding="utf-8") as stage_file:
                    stage_file.write(durable_line)
            if marker_stage and (" end" in line or " skipped" in line):
                current_stage = None
    return process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.scripts.ingest_hook.stage_log_runner"
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--runner", choices=tuple(_RUNNER_MODULES), default="ingest")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    runner_args = tuple(args.runner_args)
    if runner_args[:1] == ("--",):
        runner_args = runner_args[1:]
    return run(
        manifest=args.manifest,
        run_id=args.run_id,
        job_name=args.job_name,
        runner=args.runner,
        runner_args=runner_args,
    )


if __name__ == "__main__":
    raise SystemExit(main())
