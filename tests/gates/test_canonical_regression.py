from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.gates.canonical_regression import (
    Baseline,
    GitMetadata,
    RegressionSummary,
    compare_summary,
    parse_junit,
    verify_baseline_binding,
)


def _baseline_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "baseline_commit": "1d428ef4aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "baseline_tree_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "collected": 3,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
        "collected_node_ids": [
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
            "tests/gates/test_example.py::test_skipped",
        ],
        "failure_node_ids": [
            "tests/gates/test_example.py::test_expected_failure",
        ],
        "error_node_ids": [],
    }


def _write_baseline(path: Path, payload: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(payload or _baseline_payload()), encoding="utf-8")
    return path


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


def test_parse_junit_preserves_full_collected_node_id_list(tmp_path: Path) -> None:
    # Given: a JUnit file with pass, fail, and skip outcomes.
    tests_root = tmp_path / "tests" / "gates"
    tests_root.mkdir(parents=True)
    (tests_root / "test_example.py").write_text("", encoding="utf-8")

    # When: the JUnit file is parsed.
    summary = parse_junit(_write_junit(tmp_path / "junit.xml"), repo_root=tmp_path)

    # Then: every collected node ID is preserved, not only failures.
    assert summary == RegressionSummary(
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        test_files=("tests/gates/test_example.py",),
        collected_node_ids=(
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
            "tests/gates/test_example.py::test_skipped",
        ),
        failure_node_ids=("tests/gates/test_example.py::test_expected_failure",),
        error_node_ids=(),
    )


def test_baseline_rejects_lineage_and_tree_mismatch_injections(tmp_path: Path) -> None:
    # Given: a baseline bound to one exact commit/tree pair.
    baseline = Baseline.load(_write_baseline(tmp_path / "baseline.json"))

    # When / Then: a non-descendant head is rejected.
    with pytest.raises(ValueError, match="not descended"):
        verify_baseline_binding(
            baseline,
            GitMetadata(
                head_commit="cccccccccccccccccccccccccccccccccccccccc",
                head_tree_digest="dddddddddddddddddddddddddddddddddddddddd",
                baseline_commit_tree_digest="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                head_descends_from_baseline=False,
            ),
        )

    # When / Then: the baseline commit resolving to a different tree is rejected.
    with pytest.raises(ValueError, match="tree digest mismatch"):
        verify_baseline_binding(
            baseline,
            GitMetadata(
                head_commit="cccccccccccccccccccccccccccccccccccccccc",
                head_tree_digest="dddddddddddddddddddddddddddddddddddddddd",
                baseline_commit_tree_digest="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                head_descends_from_baseline=True,
            ),
        )


def test_compare_summary_names_removed_collected_tests() -> None:
    # Given: a baseline with the complete collected node-ID list.
    baseline = Baseline(
        baseline_commit="1d428ef4aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        baseline_tree_digest="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        collected_node_ids=(
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
            "tests/gates/test_example.py::test_removed",
        ),
        failure_node_ids=("tests/gates/test_example.py::test_expected_failure",),
        error_node_ids=(),
    )
    actual = RegressionSummary(
        collected=2,
        passed=1,
        failed=1,
        errors=0,
        skipped=0,
        test_files=("tests/gates/test_example.py",),
        collected_node_ids=(
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
        ),
        failure_node_ids=("tests/gates/test_example.py::test_expected_failure",),
        error_node_ids=(),
    )

    # When: the actual suite is compared with the baseline.
    verdict = compare_summary(actual, baseline)

    # Then: removed tests are named instead of reported only as a count delta.
    assert verdict.ok is False
    assert verdict.missing_collected == ("tests/gates/test_example.py::test_removed",)


def test_compare_summary_rejects_replaced_failure_even_when_count_is_unchanged() -> None:
    # Given: the failed-test count is unchanged but the failed node ID changed.
    baseline = Baseline(
        baseline_commit="1d428ef4aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        baseline_tree_digest="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        collected=3,
        passed=1,
        failed=1,
        errors=0,
        skipped=1,
        collected_node_ids=(
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
            "tests/gates/test_example.py::test_replacement_failure",
        ),
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
        collected_node_ids=(
            "tests/gates/test_example.py::test_expected_failure",
            "tests/gates/test_example.py::test_passed",
            "tests/gates/test_example.py::test_replacement_failure",
        ),
        failure_node_ids=("tests/gates/test_example.py::test_replacement_failure",),
        error_node_ids=(),
    )

    # When: the actual suite is compared with the baseline.
    verdict = compare_summary(actual, baseline)

    # Then: substitution is reported as both missing and unexpected failures.
    assert verdict.ok is False
    assert verdict.missing_failures == ("tests/gates/test_example.py::test_expected_failure",)
    assert verdict.unexpected_failures == ("tests/gates/test_example.py::test_replacement_failure",)
