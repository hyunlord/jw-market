"""Event-driven wake-up: kick the pipeline orchestrator after a successful ETL load.

TRANSITIONAL (2026-07-17 PL scope decision): the hook-driven input system
(JW_Input_Detection_Contract_v2) is the canonical trigger architecture and
calls ``pipeline.orchestrator`` directly; this kick is a manual/interim
fallback slated for replacement when the hook system lands.

Design contract (event-driven round, 2026-07-17):

* The kick is a WAKE-UP only. The orchestrator's own epoch/coverage detection
  decides what (if anything) actually runs, so a spurious kick is a no-op.
* Fail-closed: run.py calls this only on the all-stages-success path, so a
  failed or aborted load never kicks (no stale propagation).
* Opt-in: without ``JW_ETL_KICK_ORCHESTRATOR=1`` this module does nothing, so
  existing ETL invocations keep their exact behavior.
* Idempotent: the Job name is date-stamped; an AlreadyExists answer from the
  API is treated as success. The daily safety-net poll CronJob covers lost
  kicks, so a kick failure logs and never fails the (already successful) load.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

KICK_ENV = "JW_ETL_KICK_ORCHESTRATOR"
KICK_CRONJOB_ENV = "JW_ETL_KICK_CRONJOB"
KICK_NAMESPACE_ENV = "JW_ETL_KICK_NAMESPACE"
KICK_MARKER_ENV = "JW_ETL_KICK_MARKER_FILE"

DEFAULT_KICK_CRONJOB = "jw-pipeline-orchestrator-poll-daily"
DEFAULT_NAMESPACE = "llmops"
DEFAULT_MARKER_FILE = "/var/lib/jw-pipeline/etl-complete-marker.json"

# (argv) -> (returncode, combined output)
CommandRunner = Callable[[list[str]], tuple[int, str]]


def _default_runner(argv: list[str]) -> tuple[int, str]:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def kick_enabled(env: dict[str, str] | None = None) -> bool:
    value = (env if env is not None else os.environ).get(KICK_ENV, "")
    return value == "1"


def kick_job_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"jw-orch-kick-{stamp}"


def write_completion_marker(params: dict, *, path: str | None = None, now: datetime | None = None) -> Path:
    marker_path = Path(path or os.environ.get(KICK_MARKER_ENV) or DEFAULT_MARKER_FILE)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "mode": params.get("mode"),
        "period": params.get("period"),
        "source": params.get("source"),
        "incremental": bool(params.get("incremental")),
    }
    tmp = marker_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(marker_path)
    return marker_path


def maybe_kick_orchestrator(
    params: dict,
    *,
    runner: CommandRunner | None = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Called from run.py on the success path only. Never raises."""

    environ = env if env is not None else dict(os.environ)
    if not kick_enabled(environ):
        return {"kick": "disabled"}

    result: dict = {"kick": "attempted"}
    try:
        marker = write_completion_marker(params, path=environ.get(KICK_MARKER_ENV), now=now)
        result["marker"] = str(marker)
    except OSError as exc:
        result["marker_error"] = str(exc)

    runner = runner or _default_runner
    cronjob = environ.get(KICK_CRONJOB_ENV) or DEFAULT_KICK_CRONJOB
    namespace = environ.get(KICK_NAMESPACE_ENV) or DEFAULT_NAMESPACE
    job_name = kick_job_name(now)
    argv = [
        "kubectl",
        "-n",
        namespace,
        "create",
        "job",
        job_name,
        f"--from=cronjob/{cronjob}",
    ]
    try:
        returncode, output = runner(argv)
    except Exception as exc:  # noqa: BLE001 - kick must never fail the load
        result.update({"kick": "error", "error": str(exc)})
        print(f"[etl] kick 실패(적재는 성공) job={job_name}: {exc}")
        return result

    if returncode == 0:
        result.update({"kick": "created", "job": job_name})
        print(f"[etl] kick 완료 job={job_name}")
    elif "AlreadyExists" in output or "already exists" in output:
        # Duplicate kick within the same day: the earlier Job owns the run
        # and the orchestrator itself is epoch-idempotent.
        result.update({"kick": "noop_already_exists", "job": job_name})
        print(f"[etl] kick 중복 no-op job={job_name}")
    else:
        result.update({"kick": "error", "job": job_name, "error": output.strip()[:500]})
        print(f"[etl] kick 실패(적재는 성공, 안전망 폴링이 회수) job={job_name}: {output.strip()[:200]}")
    return result
