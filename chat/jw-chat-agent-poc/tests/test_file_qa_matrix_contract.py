from __future__ import annotations

import ast
import json
from pathlib import Path


MATRIX_PATH = Path(__file__).parent / "contracts" / "file_qa_matrix.json"


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_file_qa_matrix_has_the_declared_nonempty_population() -> None:
    matrix = _matrix()
    rows = matrix["rows"]

    assert matrix["population"] == 28
    assert len(rows) == matrix["population"]
    assert len({row["id"] for row in rows}) == matrix["population"]
    assert {row["status"] for row in rows} == {"green"}


def test_file_qa_matrix_keeps_all_user_acceptance_aliases() -> None:
    rows = {row["id"]: row for row in _matrix()["rows"]}

    assert rows["Q15"]["behavior"] == "whole-document summary alias"
    assert rows["P1"]["behavior"] == "whole-document summary"
    assert rows["Q16"]["behavior"] == "file and market comparison alias"
    assert rows["C1"]["behavior"] == "PDF and XLSX cross-check"


def _direct_test_exists(node_id: str) -> bool:
    relative_path, function_name = node_id.split("::", maxsplit=1)
    test_path = Path(__file__).parent.parent / relative_path
    if not test_path.is_file():
        return False
    module = ast.parse(test_path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
        for node in module.body
    )


def test_green_rows_name_existing_direct_tests() -> None:
    rows = _matrix()["rows"]

    assert all("::test_" in row["direct_test"] for row in rows)
    assert all(_direct_test_exists(row["direct_test"]) for row in rows)
    assert _matrix()["silent_fallback"] == "fail"


def test_green_rows_are_backed_by_the_exact_live_235_observation() -> None:
    matrix = _matrix()
    rows = matrix["rows"]
    evidence = matrix["live_evidence"]

    assert all(row.get("live_status") == "measured_pass" for row in rows)
    assert evidence["checked"] == evidence["population"] == 28
    assert evidence["semantic_passed"] == 28
    assert evidence["cleanup_residual"] == 0
    assert evidence["secret_scan"] == "NO_MATCH"
    assert evidence["git_sha"] == "da3fc1534deabbd6c0a135e4ad3f5fa63e59cde0"
    assert len(evidence["audit_sha256"]) == 64
