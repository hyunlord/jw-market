from __future__ import annotations

import ast
import csv
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "pipeline" / "scripts" / "gates" / "any_inventory.tsv"
VALIDATION_MARKERS = ("gate", "verify", "validation", "contract", "acceptance")


def _tracked_python_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "tests/**/*.py",
            "pipeline/scripts/**/*.py",
            "scripts/**/*.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    selected: list[Path] = []
    for relative_text in result.stdout.splitlines():
        relative = Path(relative_text)
        lowered = relative.as_posix().lower()
        marker_count = sum(marker in lowered for marker in VALIDATION_MARKERS)
        if lowered.startswith("pipeline/scripts/gates/") or marker_count > 0:
            selected.append(relative)
    return selected


def _any_calls() -> set[str]:
    calls: set[str] = set()
    for relative in _tracked_python_files():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "any":
                calls.add(f"{relative.as_posix()}:{node.lineno}")
    return calls


def test_every_gate_or_validation_any_call_has_an_explicit_classification() -> None:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert {row["file_line"] for row in rows} == _any_calls()
    assert {row["classification"] for row in rows} <= {"defect", "normal", "undetermined"}
    for row in rows:
        assert row["basis"]
        assert row["remediation"]


def test_no_known_any_defect_remains_unfixed() -> None:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert [row for row in rows if row["classification"] == "defect"] == []
