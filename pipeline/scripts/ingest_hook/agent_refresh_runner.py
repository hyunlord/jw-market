"""Run agent-derived refreshes after numeric ingest has committed."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone

from pipeline.scripts.ingest_hook import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(
    *,
    epoch: str,
    category: str,
    manifest_sha: str,
    ingest_run_id: str,
    agent_run_id: str | None = None,
) -> int:
    ledger = config.open_configured_ledger()
    run_id = agent_run_id or f"{ingest_run_id}:agent-refresh"
    started_at = _now()
    ledger.record_stage(
        epoch,
        category,
        manifest_sha,
        run_id=run_id,
        seq=1,
        stage="agent_refresh",
        status="running",
        started_at=started_at,
    )
    command = [
        sys.executable,
        "-m",
        "pipeline.orchestrator",
        "run",
        "--mode",
        "incremental",
        "--profile",
        "agent",
        "--force",
        "--run-id",
        run_id.replace(":", "-"),
    ]
    try:
        result = subprocess.run(command, check=False)
        returncode = result.returncode
        reason = None if returncode == 0 else f"orchestrator rc={returncode}"
    except Exception as exc:
        returncode = 1
        reason = f"{type(exc).__name__}: {exc}"
    finished_at = _now()
    status = "complete" if returncode == 0 else "failed"
    ledger.record_stage(
        epoch,
        category,
        manifest_sha,
        run_id=run_id,
        seq=1,
        stage="agent_refresh",
        status=status,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
    )
    if returncode == 0:
        for seq, stage in enumerate(("agent3", "agent2", "dashboard"), start=2):
            ledger.record_stage(
                epoch,
                category,
                manifest_sha,
                run_id=run_id,
                seq=seq,
                stage=stage,
                status="complete",
                reason="derived from successful aggregate agent_refresh; substage timing unavailable",
                started_at=finished_at,
                finished_at=finished_at,
                duration_ms=0,
            )
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--ingest-run-id", required=True)
    parser.add_argument("--agent-run-id")
    args = parser.parse_args(argv)
    return run(
        epoch=args.epoch,
        category=args.category,
        manifest_sha=args.manifest_sha,
        ingest_run_id=args.ingest_run_id,
        agent_run_id=args.agent_run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
