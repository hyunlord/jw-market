from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pipeline.scripts.crawler.crawl_chain import Stage, _timeout_for


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "pipeline" / "scripts" / "crawler" / "crawl_chain.py"
STAGES = (
    "tier1_collect",
    "tier1_classify_incremental",
    "tier2_collect_exact",
    "tier2_classify_v2_and_refresh",
)


def _fake_stage_script(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "crawl_chain_steps.sh"
    script.write_text(
        """#!/bin/sh
set -eu
stage="$1"
printf '%s\\n' "$stage" >> "$CHAIN_TEST_LOG"
mkdir -p "$CHAIN_STAGE_OUTPUT_DIR"
printf '%s\\n' "$stage" > "$CHAIN_STAGE_OUTPUT_DIR/result.txt"
if [ "${CHAIN_TEST_FAIL_STAGE:-}" = "$stage" ]; then
  exit 41
fi
cat > "$CHAIN_STAGE_OUTPUT_DIR/stage_gate.json" <<EOF
{"schema":"crawl-stage-gate/v1","stage":"$stage","exit_code":0,"failures":${CHAIN_TEST_REPORTED_FAILURES:-0},"events_raw_gap":${CHAIN_TEST_EVENTS_RAW_GAP:-0},"pending_gap":${CHAIN_TEST_PENDING_GAP:-0}}
EOF
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(
    tmp_path: Path,
    *args: str,
    fail_stage: str = "",
    reported_failures: int = 0,
    events_raw_gap: int = 0,
    pending_gap: int = 0,
) -> subprocess.CompletedProcess[str]:
    stage_script = _fake_stage_script(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "CHAIN_TEST_LOG": str(tmp_path / "stages.log"),
            "CHAIN_TEST_FAIL_STAGE": fail_stage,
            "CHAIN_TEST_REPORTED_FAILURES": str(reported_failures),
            "CHAIN_TEST_EVENTS_RAW_GAP": str(events_raw_gap),
            "CHAIN_TEST_PENDING_GAP": str(pending_gap),
            "CRAWL_CHAIN_COMMAND_REVISION": "test-revision",
            "CRAWL_CHAIN_TIMEOUT_TIER1_COLLECT": "5",
            "CRAWL_CHAIN_TIMEOUT_TIER1_CLASSIFY": "5",
            "CRAWL_CHAIN_TIMEOUT_TIER2_COLLECT": "5",
            "CRAWL_CHAIN_TIMEOUT_TIER2_CLASSIFY": "5",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "run",
            "--run-id",
            "2026-07-21T03-10-00+09-00",
            "--state-root",
            str(tmp_path / "state"),
            "--stage-script",
            str(stage_script),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _stage_log(tmp_path: Path) -> list[str]:
    path = tmp_path / "stages.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_tier2_stage_timeout_leaves_temporal_cleanup_margin(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CRAWL_CHAIN_TIMEOUT_TIER2_COLLECT", raising=False)

    assert _timeout_for(Stage.TIER2_COLLECT) == 57_000


def test_stage_timeout_terminates_descendant_processes(tmp_path: Path) -> None:
    # Given: a stage shell owns a long-running Python descendant.
    child_pid_path = tmp_path / "child.pid"
    stage_script = tmp_path / "crawl_chain_steps.sh"
    stage_script.write_text(
        """#!/bin/sh
set -eu
"$CHAIN_TEST_PYTHON" -c 'import os,time; open(os.environ["CHAIN_TEST_CHILD_PID"], "w").write(str(os.getpid())); time.sleep(60)' &
wait
""",
        encoding="utf-8",
    )
    stage_script.chmod(0o755)
    env = {
        **os.environ,
        "CHAIN_TEST_PYTHON": sys.executable,
        "CHAIN_TEST_CHILD_PID": str(child_pid_path),
        "CRAWL_CHAIN_TIMEOUT_TIER1_COLLECT": "1",
    }

    try:
        # When: the durable runner reaches its timeout.
        result = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "run-stage",
                "--run-id",
                "2026-07-24T23-00-00+09-00",
                "--state-root",
                str(tmp_path / "state"),
                "--stage-script",
                str(stage_script),
                "--stage",
                STAGES[0],
                "--command-revision",
                "cleanup-test",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 124
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        # Then: the shell's descendant is gone as well, not orphaned under PID 1.
        deadline = time.monotonic() + 2
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_exists(child_pid)
    finally:
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            if _pid_exists(child_pid):
                os.kill(child_pid, signal.SIGKILL)


def test_chain_stops_after_first_failed_stage_and_records_receipt(tmp_path: Path) -> None:
    # Given: the second stage is forced to fail.
    result = _run(tmp_path, fail_stage=STAGES[1])

    # When: the chain runs.
    receipts = tmp_path / "state" / "runs" / "2026-07-21T03-10-00+09-00" / "receipts"

    # Then: no downstream stage runs and the failure is durable.
    assert result.returncode == 41
    assert _stage_log(tmp_path) == list(STAGES[:2])
    failed = json.loads((receipts / f"{STAGES[1]}.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 41
    final_output = receipts.parent / "outputs" / STAGES[1]
    attempt_output = receipts.parent / "attempts" / STAGES[1] / "attempt-1"
    assert not final_output.exists()
    assert (attempt_output / "result.txt").exists()
    assert '"event":"CHAIN_STAGE_FAILED"' in result.stdout


def test_resume_reuses_verified_receipts_and_reruns_failed_stage(tmp_path: Path) -> None:
    # Given: stage two failed after stage one completed.
    first = _run(tmp_path, fail_stage=STAGES[1])
    assert first.returncode == 41

    # When: the same run resumes from stage two.
    resumed = _run(tmp_path, "--resume", "--from-stage", STAGES[1])

    # Then: stage one is not repeated and the remaining stages complete in order.
    assert resumed.returncode == 0
    assert _stage_log(tmp_path) == [STAGES[0], STAGES[1], *STAGES[1:]]
    assert '"event":"CHAIN_RUN_COMPLETE"' in resumed.stdout


def test_resume_of_completed_run_is_idempotent(tmp_path: Path) -> None:
    # Given: all stages have completed once.
    first = _run(tmp_path)
    assert first.returncode == 0
    before = _stage_log(tmp_path)

    # When: the run is resumed from the first stage.
    replay = _run(tmp_path, "--resume", "--from-stage", STAGES[0])

    # Then: all verified complete stages are skipped.
    assert replay.returncode == 0
    assert _stage_log(tmp_path) == before
    assert replay.stdout.count('"event":"CHAIN_STAGE_SKIPPED_COMPLETE"') == 4


def test_receipts_chain_each_stage_to_the_previous_output_hash(tmp_path: Path) -> None:
    # Given: a complete four-stage run.
    result = _run(tmp_path)
    assert result.returncode == 0
    receipts = (
        tmp_path
        / "state"
        / "runs"
        / "2026-07-21T03-10-00+09-00"
        / "receipts"
    )

    # When: the durable receipts are inspected.
    payloads = [
        json.loads((receipts / f"{stage}.json").read_text(encoding="utf-8"))
        for stage in STAGES
    ]

    # Then: the first stage has a root input and every later stage is bound to
    # the exact output accepted from its predecessor.
    assert payloads[0]["input_sha256"] == "root"
    for previous, current in zip(payloads, payloads[1:]):
        assert current["input_sha256"] == previous["output_sha256"]


def test_resume_fails_closed_when_prior_output_hash_changed(tmp_path: Path) -> None:
    # Given: stage one completed, then its durable artifact was modified.
    first = _run(tmp_path, fail_stage=STAGES[1])
    assert first.returncode == 41
    output = (
        tmp_path
        / "state"
        / "runs"
        / "2026-07-21T03-10-00+09-00"
        / "outputs"
        / STAGES[0]
        / "result.txt"
    )
    output.write_text("tampered\n", encoding="utf-8")

    # When: resume attempts to trust the prior receipt.
    resumed = _run(tmp_path, "--resume", "--from-stage", STAGES[1])

    # Then: the chain refuses to run any additional stage.
    assert resumed.returncode != 0
    assert _stage_log(tmp_path) == list(STAGES[:2])
    assert "receipt output hash mismatch" in resumed.stderr


def test_status_reports_failed_stage_without_external_alert_receiver(tmp_path: Path) -> None:
    # Given: a durable run with a failed second stage.
    failed = _run(tmp_path, fail_stage=STAGES[1])
    assert failed.returncode == 41

    # When: an operator queries the local receipt store.
    status = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "status",
            "--run-id",
            "2026-07-21T03-10-00+09-00",
            "--state-root",
            str(tmp_path / "state"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: the command is non-zero and identifies the failed stage.
    payload = json.loads(status.stdout)
    assert status.returncode == 1
    assert payload["run_status"] == "failed"
    assert payload["failed_stage"] == STAGES[1]
    assert payload["completed_stages"] == [STAGES[0]]


def test_status_fails_cleanly_when_a_receipt_is_corrupt(tmp_path: Path) -> None:
    # Given: a receipt was truncated on durable storage.
    receipt = (
        tmp_path
        / "state"
        / "runs"
        / "2026-07-21T03-10-00+09-00"
        / "receipts"
        / f"{STAGES[0]}.json"
    )
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{", encoding="utf-8")

    # When: status is queried.
    status = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "status",
            "--run-id",
            "2026-07-21T03-10-00+09-00",
            "--state-root",
            str(tmp_path / "state"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: operators receive a bounded non-zero result, not a Python traceback.
    assert status.returncode == 2
    assert "Traceback" not in status.stderr


def test_chain_rejects_exit_zero_when_stage_reports_failures(tmp_path: Path) -> None:
    result = _run(tmp_path, reported_failures=1)

    assert result.returncode != 0
    assert _stage_log(tmp_path) == [STAGES[0]]
    receipt = json.loads(
        (
            tmp_path
            / "state"
            / "runs"
            / "2026-07-21T03-10-00+09-00"
            / "receipts"
            / f"{STAGES[0]}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "reported_failures"
    assert receipt["failures"] == 1


def test_chain_rejects_zero_exit_when_either_gap_is_nonzero(tmp_path: Path) -> None:
    events_gap = _run(tmp_path / "events", events_raw_gap=3)
    pending = _run(tmp_path / "pending", pending_gap=2)

    assert events_gap.returncode != 0
    assert pending.returncode != 0
    assert _stage_log(tmp_path / "events") == [STAGES[0]]
    assert _stage_log(tmp_path / "pending") == [STAGES[0]]
