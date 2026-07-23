"""Run an ingest Job while teeing stdout/stderr into durable per-stage logs."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from pipeline.scripts.ingest_hook import config, stage_logs

_STAGE_MARKER = re.compile(
    r"^\[stage\]\s+([a-z0-9_]+)\s+(?:start|end|skipped)\b"
)


def run(*, manifest: Path, run_id: str, job_name: str) -> int:
    root = config.log_root()
    full_path = stage_logs.full_log_path(root, job_name=job_name)
    stage_logs.ensure_log_file(full_path)
    command = [
        sys.executable,
        "-m",
        "pipeline.scripts.ingest_hook.job_runner",
        "--manifest",
        str(manifest),
        "--run-id",
        run_id,
    ]
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
    args = parser.parse_args(argv)
    return run(manifest=args.manifest, run_id=args.run_id, job_name=args.job_name)


if __name__ == "__main__":
    raise SystemExit(main())
