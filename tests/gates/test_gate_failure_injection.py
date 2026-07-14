from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "pipeline" / "scripts" / "gates" / "release_acceptance.py"
COLLECTOR = ROOT / "pipeline" / "scripts" / "gates" / "collect_strict_logs.sh"
ALL_FOUR = ROOT / "pipeline" / "scripts" / "gates" / "all_four_v2_verify_audit.sh"
F062_POPULATION = ROOT / "pipeline" / "scripts" / "gates" / "f062_population_gate.sh"
INVENTORY = ROOT / "pipeline" / "scripts" / "gates" / "or_true_inventory.tsv"
ACCEPTANCE_FIELDS = (
    "gate=",
    "classification=",
    "checked=",
    "population=",
    "missing=",
    "tolerance=",
    "failures=",
    "exit_code=",
    "environment=",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_acceptance(result: subprocess.CompletedProcess[str]) -> None:
    for field in ACCEPTANCE_FIELDS:
        assert field in result.stdout


def test_i1_all_four_wrapper_uses_live_tracked_registry() -> None:
    source = ALL_FOUR.read_text(encoding="utf-8")

    assert "live-goldens" in source
    assert "tests/gates/chat_backend_live_goldens.json" in source
    assert "production_" + "goldens.tsv" not in source
    assert "golden" + "-tsv" not in source


@pytest.mark.parametrize("gate_id", ["f062_molecule_parity", "f062_corpus_parity"])
def test_i2_empty_independent_population_fails(tmp_path: Path, gate_id: str) -> None:
    candidates = _write_json(tmp_path / f"{gate_id}-candidates.json", [])
    census = _write_json(
        tmp_path / f"{gate_id}-census.json",
        {
            "population": 0,
            "source": "independent direct mart SQL",
            "source_kind": "direct_db_count",
            "query": "SELECT COUNT(*) FROM independent_expected_population",
        },
    )

    result = subprocess.run(
        [
            "bash",
            str(F062_POPULATION),
            gate_id,
            str(candidates),
            str(census),
            "failure-injection",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert f"gate={gate_id}" in result.stdout
    assert "checked=0" in result.stdout
    assert "population=0" in result.stdout
    assert "failures=1" in result.stdout
    _assert_acceptance(result)


def test_f062_population_rejects_api_derived_census(tmp_path: Path) -> None:
    candidates = _write_json(tmp_path / "candidates.json", [{"id": "one"}])
    census = _write_json(
        tmp_path / "census.json",
        {"population": 1, "source": "same filter-options API response"},
    )

    result = _run(
        "population",
        "--gate-id",
        "f062_molecule_parity",
        "--candidates",
        str(candidates),
        "--census",
        str(census),
    )

    assert result.returncode == 1
    assert "requires source_kind=direct_db_count" in result.stdout
    _assert_acceptance(result)


def test_i3_fake_error_fails_and_reports_every_pod(tmp_path: Path) -> None:
    pod_a = tmp_path / "pod-a.log"
    pod_b = tmp_path / "pod-b.log"
    pod_a.write_text("request completed 200\n", encoding="utf-8")
    pod_b.write_text("ERROR injected failure\n", encoding="utf-8")

    result = _run(
        "strict-logs",
        "--expected-pod",
        "pod-a",
        "--expected-pod",
        "pod-b",
        "--pod-log",
        f"pod-a={pod_a}",
        "--pod-log",
        f"pod-b={pod_b}",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "strict_log_matches=1" in result.stdout
    assert "strict_log_pods_scanned=2" in result.stdout
    assert "pod-b:ERROR injected failure" in result.stdout
    _assert_acceptance(result)


def test_strict_log_collector_scans_every_deployment_pod(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if "deployment" in args:
    print(json.dumps({"spec": {"selector": {"matchLabels": {"app": "demo"}}}}))
elif "pods" in args:
    print(json.dumps({"items": [
        {"metadata": {"name": "pod-a"}},
        {"metadata": {"name": "pod-b"}},
    ]}))
elif "logs" in args:
    pod = args[args.index("logs") + 1]
    print("ERROR injected failure" if pod == "pod-b" else "request completed 200")
else:
    raise SystemExit(64)
""",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        [
            "bash",
            str(COLLECTOR),
            "llmops",
            "demo",
            "30m",
            str(tmp_path / "logs"),
            "failure-injection",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "strict_log_matches=1" in result.stdout
    assert "strict_log_pods_scanned=2" in result.stdout
    assert "checked=2" in result.stdout
    assert "population=2" in result.stdout


def test_g4_normal_inputs_pass_without_false_positive(tmp_path: Path) -> None:
    candidates = _write_json(tmp_path / "candidates.json", [{"id": "one"}])
    census = _write_json(
        tmp_path / "census.json",
        {
            "population": 1,
            "source": "independent direct mart SQL",
            "source_kind": "direct_db_count",
            "query": "SELECT COUNT(*) FROM independent_expected_population",
        },
    )
    clean_log = tmp_path / "pod-a.log"
    clean_log.write_text("request completed 200\n", encoding="utf-8")
    population = _run(
        "population",
        "--gate-id",
        "f062_molecule_parity",
        "--candidates",
        str(candidates),
        "--census",
        str(census),
    )
    logs = _run(
        "strict-logs",
        "--expected-pod",
        "pod-a",
        "--pod-log",
        f"pod-a={clean_log}",
    )
    assert population.returncode == 0
    assert logs.returncode == 0
    assert "strict_log_matches=0" in logs.stdout
    _assert_acceptance(population)
    _assert_acceptance(logs)


def test_or_true_inventory_is_complete_and_has_no_unresolved_defect() -> None:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert len(rows) == 30
    assert [int(row["id"]) for row in rows] == list(range(1, 31))
    assert {row["classification"] for row in rows} <= {
        "observational",
        "defect",
        "undetermined",
    }
    for row in rows:
        assert row["command"]
        assert row["basis"]
        if row["classification"] == "defect":
            assert row["remediation"]
