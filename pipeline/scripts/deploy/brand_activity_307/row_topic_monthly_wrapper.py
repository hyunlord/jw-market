from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any

import pymysql


SCHEMA = os.environ.get("ROW_TOPIC_SCHEMA", "jw_brand_activity_stage")
DEFAULT_MAX_CALLS = int(os.environ.get("ROW_TOPIC_MAX_CALLS", "350"))
GATE_MAX_CALLS = int(os.environ.get("ROW_TOPIC_GATE_MAX_CALLS", "5"))


@dataclass(frozen=True, slots=True)
class RowTopicRunResult:
    pending_rows: int
    calls: int
    inserts: int


def _prepare_environment() -> Path:
    project_root = Path(os.environ.get("PROJECT_ROOT", "/app"))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    os.environ.setdefault("MARIADB_HOST", os.environ.get("DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local"))
    os.environ.setdefault("MARIADB_PORT", os.environ.get("DB_PORT", "3306"))
    os.environ.setdefault("MARIADB_USER", os.environ.get("DB_USER", "llmops"))
    # The row-topic runner's connection helper reads MARIADB_ROOT_PASSWORD.
    # In this job it intentionally carries the llmops account password.
    if not os.environ.get("MARIADB_ROOT_PASSWORD"):
        password = os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD")
        if password:
            os.environ["MARIADB_ROOT_PASSWORD"] = password
    return project_root


def _connect() -> pymysql.connections.Connection:
    password = os.environ.get("MARIADB_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("DB password env is missing")
    return pymysql.connect(
        host=os.environ["MARIADB_HOST"],
        port=int(os.environ["MARIADB_PORT"]),
        user=os.environ["MARIADB_USER"],
        password=password,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _latest_topic_set_version() -> str:
    sql = f"""
        SELECT run_id
        FROM `{SCHEMA}`.`mart_brand_activity_topic_runs`
        WHERE input_fingerprint IS NOT NULL AND input_fingerprint <> ''
        ORDER BY created_at DESC, updated_at DESC
        LIMIT 1
    """
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
    if not row:
        raise RuntimeError("no topic run with input_fingerprint found")
    return str(row["run_id"])


def _mode_from_job_name() -> str:
    explicit = os.environ.get("GATE_MODE", "").strip().lower()
    if explicit:
        return explicit
    job_name = os.environ.get("JOB_NAME", "").lower()
    if "dryrun" in job_name or "dry-run" in job_name:
        return "dry-run"
    if "execute" in job_name or "live" in job_name:
        return "execute"
    return "auto"


def _run_row_topic(
    mode: str,
    version: str,
    max_calls: int | None = None,
    *,
    affected_scope: dict[str, object] | None = None,
    run_id: str = "monthly",
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "pipeline.scripts.analysis.brand_activity.row_topic.execute",
        mode,
        "--schema",
        SCHEMA,
        "--topic-set-version",
        version,
        "--pending-source",
        "db",
        "--checkpoint",
        f"/tmp/row_topic_assignment_checkpoint_{run_id}.jsonl",
        "--log",
        f"/tmp/row_topic_assignment_execute_log_{run_id}.jsonl",
    ]
    if max_calls is not None:
        cmd.extend(["--max-calls", str(max_calls)])
    if affected_scope is not None:
        cmd.extend(["--affected-scope-json", json.dumps(affected_scope, sort_keys=True, separators=(",", ":"))])
    completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"row-topic {mode} failed with exit {completed.returncode}")
    return _last_json(completed.stdout)


def run_for_ingest(
    *,
    affected_scope: dict[str, object],
    category: str,
    epoch: str,
    manifest_sha: str,
    run_id: str,
) -> RowTopicRunResult:
    """Run the existing row-topic path for only the newly published periods."""
    _prepare_environment()
    version = os.environ.get("ROW_TOPIC_SET_VERSION") or _latest_topic_set_version()
    print(json.dumps({
        "event": "row_topic_ingest_start",
        "category": category,
        "epoch": epoch,
        "manifest_sha": manifest_sha,
        "run_id": run_id,
        "affected_scope": affected_scope,
        "topic_set_version": version,
    }, sort_keys=True), flush=True)
    dry = _run_row_topic("dry-run", version, affected_scope=affected_scope, run_id=run_id)
    pending_rows = int(dry.get("pending_rows") or 0)
    if pending_rows == 0:
        return RowTopicRunResult(pending_rows=0, calls=0, inserts=0)
    if not os.environ.get("GENOS_BEARER_TOKEN"):
        raise RuntimeError("GENOS_BEARER_TOKEN is required before executing pending row-topic calls")
    result = _run_row_topic(
        "execute",
        version,
        max_calls=GATE_MAX_CALLS,
        affected_scope=affected_scope,
        run_id=run_id,
    )
    return RowTopicRunResult(
        pending_rows=pending_rows,
        calls=int(result.get("calls_used") or 0),
        inserts=int(result.get("assignment_rows_inserted_or_updated") or 0),
    )


def _last_json(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return json.loads(stripped)
    raise RuntimeError("row-topic output did not contain a JSON object")


def main() -> int:
    project_root = _prepare_environment()
    os.chdir(project_root)
    mode = _mode_from_job_name()
    if mode not in {"dry-run", "execute", "auto"}:
        raise RuntimeError(f"unsupported GATE_MODE: {mode}")
    version = os.environ.get("ROW_TOPIC_SET_VERSION") or _latest_topic_set_version()
    print(json.dumps({"event": "row_topic_gate_start", "mode": mode, "topic_set_version": version}, sort_keys=True), flush=True)
    dry = _run_row_topic("dry-run", version)
    pending_rows = int(dry.get("pending_rows") or 0)
    pending_batches = int(dry.get("pending_batches") or 0)
    print(
        json.dumps(
            {
                "event": "row_topic_pending_gate",
                "mode": mode,
                "topic_set_version": version,
                "pending_rows": pending_rows,
                "pending_batches": pending_batches,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if mode == "dry-run":
        return 0
    if pending_rows == 0:
        print(json.dumps({"event": "row_topic_noop", "calls": 0, "inserts": 0}, sort_keys=True), flush=True)
        return 0
    if not os.environ.get("GENOS_BEARER_TOKEN"):
        raise RuntimeError("GENOS_BEARER_TOKEN is required before executing pending row-topic calls")
    cap = GATE_MAX_CALLS if mode == "execute" else DEFAULT_MAX_CALLS
    result = _run_row_topic("execute", version, max_calls=cap)
    print(json.dumps({"event": "row_topic_execute_complete", "result": result}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
