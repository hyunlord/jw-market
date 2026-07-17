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
    assert {row["status"] for row in rows} == {"implemented_unmeasured", "red"}


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


def test_implemented_rows_name_existing_direct_tests_and_red_rows_are_explicit() -> None:
    rows = _matrix()["rows"]
    implemented = [row for row in rows if row["status"] == "implemented_unmeasured"]
    red = [row for row in rows if row["status"] == "red"]

    assert len(implemented) == 14
    assert len(red) == 14
    assert all("::test_" in row["direct_test"] for row in implemented)
    assert all(_direct_test_exists(row["direct_test"]) for row in implemented)
    assert all(row["direct_test"] is None for row in red)
    assert _matrix()["silent_fallback"] == "fail"


def test_file_qa_rows_cannot_claim_green_before_live_235_observation() -> None:
    rows = _matrix()["rows"]

    assert all(row.get("live_status") == "unmeasured" for row in rows)
    assert all(row["status"] != "green" for row in rows)
