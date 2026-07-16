from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from test_post_reload_mart_gate import (
    DATABASE,
    EXPECTED_GATES,
    MARKER,
    _valid_evidence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = PROJECT_ROOT / "scripts" / "post_reload_mart_gate.py"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _identity_args() -> tuple[str, ...]:
    return (
        "--reload-run-id",
        "dimfix5_20260716",
        "--database",
        DATABASE,
        "--fdm-computed-at",
        MARKER,
    )


def test_cli_accepts_bound_evidence_and_prints_seven_acceptance_lines(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_valid_evidence(), ensure_ascii=False), encoding="utf-8")

    result = _run("--evidence", str(evidence_path), *_identity_args())

    assert result.returncode == 0
    assert result.stdout.count("gate=") == len(EXPECTED_GATES)
    assert all(f"gate={gate}" in result.stdout for gate in EXPECTED_GATES)
    assert "exit_code=0" in result.stdout


def test_cli_refuses_runtime_query_before_reload_completion() -> None:
    env = os.environ.copy()
    env.pop("MART_RELOAD_COMPLETE", None)

    result = _run(*_identity_args(), env=env)

    assert result.returncode == 1
    assert "gate=mart_reload_authorization" in result.stdout
    assert "reload_completion_not_authorized" in result.stdout


def test_cli_refuses_runtime_query_without_complete_identity() -> None:
    env = os.environ.copy()
    env["MART_RELOAD_COMPLETE"] = "1"
    for name in ("MART_RELOAD_RUN_ID", "MART_RELOAD_DB_NAME", "MART_FDM_COMPUTED_AT"):
        env.pop(name, None)

    result = _run(env=env)

    assert result.returncode == 1
    assert "gate=mart_reload_authorization" in result.stdout
    assert "failure_count=3" in result.stdout


def test_cli_failure_injection_returns_one_for_zero_population(tmp_path: Path) -> None:
    evidence = _valid_evidence()
    evidence["market_rows"] = []
    evidence_path = tmp_path / "zero-population.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")

    result = _run("--evidence", str(evidence_path), *_identity_args())

    assert result.returncode == 1
    assert "gate=general_dimension_parity" in result.stdout
    assert "population=0" in result.stdout


def test_cli_rejects_evidence_from_another_marker(tmp_path: Path) -> None:
    evidence = _valid_evidence()
    evidence["fdm_marker_rows"][0]["computed_at_min"] = "2026-07-15T00:00:00Z"  # type: ignore[index]
    evidence_path = tmp_path / "stale.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")

    result = _run("--evidence", str(evidence_path), *_identity_args())

    assert result.returncode == 1
    assert "marker_mismatch" in result.stdout
