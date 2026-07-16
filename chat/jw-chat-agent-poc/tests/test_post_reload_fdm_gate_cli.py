from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = PROJECT_ROOT / "scripts" / "post_reload_fdm_gate.py"
DATABASE = "jw_mart_d2_stage_20260630_r2"
MARKER = "2026-07-16T08:26:03Z"


def _history(first: float, second: float) -> dict[str, dict[str, float]]:
    return {
        "2026-04": {"raw_value": first},
        "2026-05": {"raw_value": second},
    }


def _evidence() -> dict[str, object]:
    dimensions = ("seller", "molecule_strength", "form", "route", "reimbursement")
    return {
        "tx_read_only": 1,
        "database": DATABASE,
        "fdm_marker_rows": [
            {
                "dimension_type": dimension,
                "row_count": 2,
                "marker_count": 1,
                "computed_at_min": MARKER,
                "computed_at_max": MARKER,
            }
            for dimension in dimensions
        ],
        "market_rows": [{"market_id": "C10A1", "market_size_series": _history(90.0, 100.0)}],
        "sidecar_rows": [
            {
                "market_id": "C10A1",
                "dimension_type": dimension,
                "raw_value_history": _history(april, may),
            }
            for dimension in dimensions
            for april, may in ((36.0, 40.0), (54.0, 60.0))
        ],
        "molecule_rows": [
            {
                "market_id": "C10A1",
                "dimension_type": "molecule",
                "raw_value_history": _history(45.0, 50.0),
            },
            {
                "market_id": "C10A1",
                "dimension_type": "molecule",
                "raw_value_history": _history(45.0, 50.0),
            },
        ],
    }


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


def test_cli_accepts_bound_evidence_and_prints_acceptance_contract(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    result = _run("--evidence", str(evidence_path), *_identity_args())

    assert result.returncode == 0
    assert result.stdout.count("gate=") == 4
    assert "gate=fdm_reload_cohort" in result.stdout
    assert "checked=5 population=5" in result.stdout
    assert "exit_code=0" in result.stdout


def test_cli_refuses_runtime_query_before_reload_completion() -> None:
    env = os.environ.copy()
    env.pop("MART_RELOAD_COMPLETE", None)

    result = _run(*_identity_args(), env=env)

    assert result.returncode == 1
    assert "gate=mart_reload_authorization" in result.stdout
    assert "reload_completion_not_authorized" in result.stdout


def test_cli_refuses_runtime_query_without_full_identity() -> None:
    env = os.environ.copy()
    env["MART_RELOAD_COMPLETE"] = "1"
    for name in ("MART_RELOAD_RUN_ID", "MART_RELOAD_DB_NAME", "MART_FDM_COMPUTED_AT"):
        env.pop(name, None)

    result = _run(env=env)

    assert result.returncode == 1
    assert "gate=mart_reload_authorization" in result.stdout
    assert "failure_count=3" in result.stdout


def test_cli_rejects_evidence_from_another_marker(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["fdm_marker_rows"][0]["computed_at_min"] = "2026-06-29T00:00:00Z"  # type: ignore[index]
    evidence_path = tmp_path / "stale.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    result = _run("--evidence", str(evidence_path), *_identity_args())

    assert result.returncode == 1
    assert "marker_mismatch" in result.stdout
