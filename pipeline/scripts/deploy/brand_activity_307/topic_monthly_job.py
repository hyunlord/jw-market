#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymysql",
# ]
# ///
"""Monthly brand-activity topic scheduler with an input fingerprint guard.

The job is intentionally conservative: without a previous successful run
fingerprint it exits non-zero, and when the current stage fingerprint matches
the last stored run it exits zero before any GenOS call can start.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Final

import pymysql


SCHEMA: Final = "jw_brand_activity_stage"
KEYWORD_TABLE: Final = "km_keyword_event_stage"
RUNS_TABLE: Final = "mart_brand_activity_topic_runs"
DEFAULT_JSON_URL: Final = "http://code-serving-238:8080/json"

JsonObject = dict[str, Any]
PostJson = Callable[[str, JsonObject, int], JsonObject]


class SchedulerError(RuntimeError):
    """Raised when the monthly scheduler cannot safely continue."""


class PreflightAction(Enum):
    """Possible preflight decisions before any topic run is started."""

    FAIL = "fail"
    NOOP = "noop"
    START = "start"


@dataclass(frozen=True, slots=True)
class StageFingerprint:
    """Current Keyword stage fingerprint."""

    row_count: int
    stage_hash_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredFingerprint:
    """Last successful topic run fingerprint seed."""

    run_id: str
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    """Cost-gate decision and evidence."""

    action: PreflightAction
    message: str
    current: StageFingerprint
    stored: StoredFingerprint | None = None


@dataclass(frozen=True, slots=True)
class JobConfig:
    """Runtime knobs for the code-serving polling job."""

    json_url: str = DEFAULT_JSON_URL
    stage_schema: str = SCHEMA
    raw_schema: str = "jw_brand_activity_raw_stage"
    max_real_calls: int = 350
    brands_per_market: int | None = None
    large_market_limit: int = 0
    request_timeout_seconds: int = 60
    poll_interval_seconds: int = 30
    max_wait_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class RunResult:
    """Final result payload condensed for scheduler logs."""

    run_id: str
    status: str
    executed_call_count: int
    artifact_sha256: str
    db_save_summary: JsonObject
    raw_payload: JsonObject


def connect_mariadb() -> pymysql.connections.Connection:
    """Open MariaDB using environment variables without logging secrets."""
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "llmops-mariadb-service"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=_required_env("MARIADB_ROOT_PASSWORD"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_stage_fingerprint(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> StageFingerprint:
    """Fingerprint stage keyword input in the same order as run_auto_topic."""
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{schema}`.`{KEYWORD_TABLE}`")
        row_count = int(cursor.fetchone()["row_count"])
        cursor.execute(f"SELECT stage_row_sha256 FROM `{schema}`.`{KEYWORD_TABLE}` ORDER BY id")
        digest = hashlib.sha256()
        for row in cursor.fetchall():
            digest.update(str(row["stage_row_sha256"]).encode("utf-8"))
            digest.update(b"\n")
        cursor.execute("COMMIT")
    return StageFingerprint(row_count=row_count, stage_hash_fingerprint=digest.hexdigest())


def fetch_last_stored_fingerprint(
    connection: pymysql.connections.Connection,
    *,
    schema: str = SCHEMA,
) -> StoredFingerprint | None:
    """Read the last successful stored run fingerprint, if the seed exists."""
    sql = f"""
        SELECT run_id, input_fingerprint
        FROM `{schema}`.`{RUNS_TABLE}`
        WHERE input_fingerprint IS NOT NULL AND input_fingerprint <> ''
        ORDER BY created_at DESC, updated_at DESC
        LIMIT 1
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
    except pymysql.err.ProgrammingError:
        return None
    if not row:
        return None
    return StoredFingerprint(run_id=str(row["run_id"]), input_fingerprint=str(row["input_fingerprint"]))


def decide_preflight(current: StageFingerprint, stored: StoredFingerprint | None) -> PreflightDecision:
    """Decide whether the monthly job may spend GenOS calls."""
    if stored is None:
        return PreflightDecision(
            action=PreflightAction.FAIL,
            message="fingerprint seed missing; refusing first monthly execute",
            current=current,
            stored=None,
        )
    if current.stage_hash_fingerprint == stored.input_fingerprint:
        return PreflightDecision(
            action=PreflightAction.NOOP,
            message="no-op: input unchanged",
            current=current,
            stored=stored,
        )
    return PreflightDecision(
        action=PreflightAction.START,
        message="input changed; starting bounded topic extraction",
        current=current,
        stored=stored,
    )


def run_topic_job(config: JobConfig, *, post_json: PostJson | None = None) -> RunResult:
    """Start one code-serving topic run, poll once it has a run_id, and fetch the result."""
    post = post_json or _post_json
    run_id = _start_run(config, post_json=post)
    status = "running"
    deadline = time.monotonic() + config.max_wait_seconds
    while time.monotonic() < deadline:
        status_payload = _call_tool(config, "get_status", {"run_id": run_id}, "monthly-status", post_json=post)
        status = str(status_payload.get("status") or "")
        _log("poll", {"run_id": run_id, "status": status})
        if status in {"done", "error", "failed"}:
            break
        time.sleep(config.poll_interval_seconds)
    else:
        raise SchedulerError(f"timed out waiting for run_id={run_id}")

    result_payload = _call_tool(config, "get_result", {"run_id": run_id}, "monthly-result", post_json=post)
    db_summary = result_payload.get("db_save_summary")
    return RunResult(
        run_id=run_id,
        status=str(result_payload.get("status") or status),
        executed_call_count=_int(result_payload.get("executed_call_count")),
        artifact_sha256=str(result_payload.get("artifact_sha256") or result_payload.get("zip_sha256") or ""),
        db_save_summary=db_summary if isinstance(db_summary, dict) else {},
        raw_payload=result_payload,
    )


def main() -> int:
    """Run the monthly scheduler once."""
    config = _config_from_env()
    with connect_mariadb() as connection:
        current = fetch_stage_fingerprint(connection, schema=config.stage_schema)
        stored = fetch_last_stored_fingerprint(connection, schema=config.stage_schema)
    decision = decide_preflight(current, stored)
    _log(
        "preflight",
        {
            "action": decision.action.value,
            "message": decision.message,
            "current_row_count": decision.current.row_count,
            "current_fingerprint": decision.current.stage_hash_fingerprint,
            "stored_run_id": decision.stored.run_id if decision.stored else None,
        },
    )
    if decision.action is PreflightAction.FAIL:
        return 1
    if decision.action is PreflightAction.NOOP:
        return 0

    result = run_topic_job(config)
    _log(
        "result",
        {
            "run_id": result.run_id,
            "status": result.status,
            "executed_call_count": result.executed_call_count,
            "artifact_sha256": result.artifact_sha256,
            "db_save_summary": result.db_save_summary,
        },
    )
    if result.status != "done" or not _db_save_ok(result.db_save_summary):
        return 1
    return 0


def _start_run(config: JobConfig, *, post_json: PostJson) -> str:
    """Start one run with a single pre-run retry only if no run_id was issued."""
    arguments: JsonObject = {
        "dry_run": False,
        "save_to_db": True,
        "max_real_calls": config.max_real_calls,
        "large_market_limit": config.large_market_limit,
        "stage_schema": config.stage_schema,
        "raw_schema": config.raw_schema,
    }
    if config.brands_per_market is not None:
        arguments["brands_per_market"] = config.brands_per_market
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            payload = _call_tool(config, "run_topic_extraction", arguments, f"monthly-start-{attempt}", post_json=post_json)
            run_id = str(payload.get("run_id") or "")
            if run_id:
                _log("started", {"run_id": run_id, "attempt": attempt})
                return run_id
            raise SchedulerError(f"run_topic_extraction response did not include run_id: {payload}")
        except Exception as exc:  # noqa: BLE001 - one retry is intentionally broad before run_id exists.
            last_error = exc
            _log("start_retry" if attempt == 1 else "start_failed", {"attempt": attempt, "error": str(exc)})
            if attempt == 1:
                time.sleep(3)
    raise SchedulerError(f"failed before run_id was issued: {last_error}") from last_error


def _call_tool(config: JobConfig, name: str, arguments: JsonObject, rpc_id: str, *, post_json: PostJson) -> JsonObject:
    """Call one MCP tool through the code-serving /json relay."""
    response = post_json(
        config.json_url,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        config.request_timeout_seconds,
    )
    if response.get("error"):
        raise SchedulerError(f"{name} returned JSON-RPC error: {response['error']}")
    return _extract_tool_payload(response)


def _extract_tool_payload(response: JsonObject) -> JsonObject:
    """Extract structured MCP content from code-serving response variants."""
    result = response.get("result")
    if not isinstance(result, dict):
        raise SchedulerError(f"missing JSON-RPC result: {response}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                value = json.loads(item["text"])
                if isinstance(value, dict):
                    return value
    raise SchedulerError(f"cannot parse MCP result payload: {response}")


def _post_json(url: str, payload: JsonObject, timeout: int) -> JsonObject:
    """POST JSON using stdlib only so the CronJob image stays lightweight."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SchedulerError(f"request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise SchedulerError(f"expected JSON object response, got: {type(value).__name__}")
    return value


def _config_from_env() -> JobConfig:
    """Build config from CronJob env, preserving the hard call cap."""
    max_real_calls = _int_env("TOPIC_MAX_REAL_CALLS", 350)
    if max_real_calls > 350:
        raise SchedulerError(f"TOPIC_MAX_REAL_CALLS may not exceed 350: {max_real_calls}")
    return JobConfig(
        json_url=os.environ.get("TOPIC_JSON_URL", DEFAULT_JSON_URL),
        stage_schema=os.environ.get("TOPIC_STAGE_SCHEMA", SCHEMA),
        raw_schema=os.environ.get("TOPIC_RAW_SCHEMA", "jw_brand_activity_raw_stage"),
        max_real_calls=max_real_calls,
        brands_per_market=_optional_int_env("TOPIC_BRANDS_PER_MARKET"),
        large_market_limit=_int_env("TOPIC_LARGE_MARKET_LIMIT", 0),
        request_timeout_seconds=_int_env("TOPIC_REQUEST_TIMEOUT_SECONDS", 60),
        poll_interval_seconds=_int_env("TOPIC_POLL_INTERVAL_SECONDS", 30),
        max_wait_seconds=_int_env("TOPIC_MAX_WAIT_SECONDS", 3600),
    )


def _db_save_ok(summary: JsonObject) -> bool:
    """Return true when DB save summary shows the run row was stored."""
    return _int(summary.get("stored_run_rows")) >= 1 and _int(summary.get("stored_topic_rows")) >= 1


def _required_env(key: str) -> str:
    """Read a required env var without exposing its value."""
    value = os.environ.get(key)
    if not value:
        raise SchedulerError(f"required environment variable missing: {key}")
    return value


def _int_env(key: str, default: int) -> int:
    """Read an integer env var with a default."""
    value = os.environ.get(key)
    return int(value) if value else default


def _optional_int_env(key: str) -> int | None:
    """Read an optional positive integer without imposing a storage cap."""
    value = os.environ.get(key)
    return int(value) if value else None


def _int(value: Any) -> int:
    """Coerce JSON numeric scalars to int."""
    return int(value) if isinstance(value, int | float) else 0


def _log(event: str, payload: JsonObject) -> None:
    """Emit a compact structured log line with no secrets."""
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    sys.exit(main())
