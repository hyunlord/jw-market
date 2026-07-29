"""Instrumentation contract.

The 255s failure left ``lines=2`` in the heartbeat and no child stdout anywhere
durable, so "which page was slow" was unanswerable after the fact. These tests
pin the two things that fixes: enriched heartbeats and a preserved stdout log.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pipeline.scripts.crawler.hira_benefit.runtime import (
    HEARTBEAT_TELEMETRY_KEYS,
    merge_telemetry,
    run_subprocess_with_heartbeat,
    stage_log_path,
)

_CHILD = (
    "import json,sys\n"
    "print('plain line before telemetry')\n"
    "print(json.dumps({'event':'hira_page_fetched','page':7,'pages_done':6,"
    "'pages_total':18,'items':30,'page_elapsed_seconds':4.31}))\n"
    "print(json.dumps({'event':'hira_page_fetched','page':8,'pages_done':7,"
    "'pages_total':18,'items':30,'page_elapsed_seconds':2.02}))\n"
    "sys.exit(0)\n"
)


def _run(tmp_path: Path, log_path: Path | None) -> tuple[int, list[dict[str, Any]]]:
    beats: list[dict[str, Any]] = []
    code = asyncio.run(
        run_subprocess_with_heartbeat(
            (sys.executable, "-c", _CHILD),
            cwd=str(tmp_path),
            heartbeat=beats.append,
            stage="discover_page_batch",
            log_path=log_path,
        )
    )
    return code, beats


def test_heartbeat_carries_page_progress_not_just_a_line_count(tmp_path: Path) -> None:
    code, beats = _run(tmp_path, None)

    assert code == 0
    assert beats, "a stage that produces output must heartbeat"
    last = beats[-1]
    assert last["stage"] == "discover_page_batch"
    assert last["page"] == 8
    assert last["pages_done"] == 7
    assert last["pages_total"] == 18
    assert last["page_elapsed_seconds"] == 2.02
    assert last["event"] == "hira_page_fetched"
    assert last["lines"] == 3
    assert isinstance(last["elapsed_seconds"], float)


def test_heartbeat_reports_the_page_that_was_in_flight(tmp_path: Path) -> None:
    """Progress must be readable mid-run, not only at the end."""

    _, beats = _run(tmp_path, None)
    pages = [beat.get("page") for beat in beats]

    assert pages == [None, 7, 8]


def test_child_stdout_is_preserved_durably(tmp_path: Path) -> None:
    log_path = stage_log_path(tmp_path, "discover_page_batch.p0002-0019")
    code, _ = _run(tmp_path, log_path)

    assert code == 0
    assert log_path.parent.name == "logs"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "plain line before telemetry"
    assert json.loads(lines[1])["page"] == 7
    assert json.loads(lines[2])["page"] == 8


def test_stdout_log_appends_across_retries(tmp_path: Path) -> None:
    """A retried attempt must not erase the failed attempt's evidence."""

    log_path = stage_log_path(tmp_path, "discover_page_batch.p0002-0019")
    _run(tmp_path, log_path)
    _run(tmp_path, log_path)

    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 6


def test_non_json_output_never_breaks_telemetry() -> None:
    telemetry: dict[str, Any] = {"stage": "collect_details"}

    assert merge_telemetry("Traceback (most recent call last):", telemetry) == telemetry
    assert merge_telemetry("{ truncated json", telemetry) == telemetry
    assert merge_telemetry("[1, 2, 3]", telemetry) == telemetry
    assert telemetry == {"stage": "collect_details"}


def test_only_whitelisted_telemetry_keys_reach_the_heartbeat() -> None:
    telemetry: dict[str, Any] = {}
    merged = merge_telemetry(
        json.dumps(
            {
                "event": "hira_http_request",
                "page": 3,
                "url": "https://www.hira.or.kr/secret?token=abc",
                "retry_count": 2,
            }
        ),
        telemetry,
    )

    assert merged["page"] == 3
    assert merged["retry_count"] == 2
    assert "url" not in merged
    assert set(merged) <= {*HEARTBEAT_TELEMETRY_KEYS, "event"}
