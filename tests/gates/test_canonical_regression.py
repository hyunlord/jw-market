from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.gates.canonical_regression import (
    Baseline,
    RegressionSummary,
    compare_summary,
    parse_junit,
)


def _write_junit(path: Path, *, failed_name: str = "test_expected_failure") -> Path:
    path.write_text(
        (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites name="pytest tests">'
            '<testsuite errors="0" failures="1" skipped="1" tests="3">'
            '<testcase classname="tests.gates.test_example" name="test_passed" />'
            f'<testcase classname="tests.gates.test_example" name="{failed_name}">'
            '<failure message="injected">traceback</failure>'
            "</testcase>"
            '<testcase classname="tests.gates.test_example" name="test_skipped">'
            '<skipped type="pytest.skip" message="not applicable" />'
            "</testcase>"
            "</testsuite>"
            "</testsuites>"
        ),
        encoding="utf-8",
    )
    return path


def test_parse_junit_preserves_counts_files_and_exact_failure_node_ids(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests" / "gates"
    tests_root.mkdir(parents=True)
    (tests_root / "test_example.py").write_text("", encoding="utf-8")

    summary = parse_junit(_write_junit(tmp_path / "junit.xml"), repo_root=tmp_path)

    assert summary == RegressionSummary(
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        test_files=("tests/gates/test_example.py",),
        failure_node_ids=("tests/gates/test_example.py::test_expected_failure",),
        error_node_ids=(),
    )


def test_compare_summary_rejects_replaced_failure_even_when_count_is_unchanged() -> None:
    baseline = Baseline(
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        failure_node_ids=("tests/gates/test_example.py::test_expected_failure",),
        error_node_ids=(),
    )
    actual = RegressionSummary(
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        test_files=("tests/gates/test_example.py",),
        failure_node_ids=("tests/gates/test_example.py::test_replacement_failure",),
        error_node_ids=(),
    )

    verdict = compare_summary(actual, baseline)

    assert verdict.ok is False
    assert verdict.missing_failures == ("tests/gates/test_example.py::test_expected_failure",)
    assert verdict.unexpected_failures == ("tests/gates/test_example.py::test_replacement_failure",)


def test_parse_junit_fails_closed_when_test_source_cannot_be_resolved(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot resolve test source"):
        parse_junit(_write_junit(tmp_path / "junit.xml"), repo_root=tmp_path)


def test_baseline_rejects_missing_required_inputs(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing baseline keys"):
        Baseline.load(path)
