from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASELINE = Path(__file__).with_name("canonical_regression_baseline.json")


@dataclass(frozen=True, slots=True)
class Baseline:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    failure_node_ids: tuple[str, ...]
    error_node_ids: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Baseline:
        payload = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "schema_version",
            "collected",
            "passed",
            "failed",
            "errors",
            "skipped",
            "failure_node_ids",
            "error_node_ids",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"missing baseline keys: {missing}")
        if payload["schema_version"] != 1:
            raise ValueError(f"unsupported baseline schema: {payload['schema_version']}")
        failure_node_ids = _validated_node_ids(payload["failure_node_ids"], "failure_node_ids")
        error_node_ids = _validated_node_ids(payload["error_node_ids"], "error_node_ids")
        counts = {
            name: payload[name]
            for name in ("collected", "passed", "failed", "errors", "skipped")
        }
        if any(not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("baseline counts must be non-negative integers")
        if counts["failed"] != len(failure_node_ids):
            raise ValueError("baseline failed count does not match failure_node_ids")
        if counts["errors"] != len(error_node_ids):
            raise ValueError("baseline errors count does not match error_node_ids")
        if sum(counts[name] for name in ("passed", "failed", "errors", "skipped")) != counts["collected"]:
            raise ValueError("baseline outcome counts do not add up to collected")
        return cls(
            **counts,
            failure_node_ids=failure_node_ids,
            error_node_ids=error_node_ids,
        )


@dataclass(frozen=True, slots=True)
class RegressionSummary:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    test_files: tuple[str, ...]
    failure_node_ids: tuple[str, ...]
    error_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    ok: bool
    count_mismatches: tuple[str, ...]
    missing_failures: tuple[str, ...]
    unexpected_failures: tuple[str, ...]
    missing_errors: tuple[str, ...]
    unexpected_errors: tuple[str, ...]


def _validated_node_ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a JSON array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(value))


def _test_source_and_class(classname: str, repo_root: Path) -> tuple[str, tuple[str, ...]]:
    parts = classname.split(".")
    for prefix_length in range(len(parts), 0, -1):
        candidate = repo_root.joinpath(*parts[:prefix_length]).with_suffix(".py")
        if candidate.is_file():
            source = candidate.relative_to(repo_root).as_posix()
            return source, tuple(parts[prefix_length:])
    raise ValueError(f"cannot resolve test source for classname={classname!r}")


def _node_id(testcase: ET.Element, repo_root: Path) -> tuple[str, str]:
    classname = testcase.get("classname")
    name = testcase.get("name")
    if not classname or not name:
        raise ValueError("testcase requires classname and name")
    source, classes = _test_source_and_class(classname, repo_root)
    suffix = "::".join((*classes, name))
    return source, f"{source}::{suffix}"


def parse_junit(path: Path, *, repo_root: Path = ROOT) -> RegressionSummary:
    if not path.is_file():
        raise ValueError(f"junit output missing: {path}")
    root = ET.parse(path).getroot()
    suites = tuple(root.iter("testsuite"))
    if not suites:
        raise ValueError("junit contains no testsuite")
    suite = suites[0]
    try:
        collected = int(suite.attrib["tests"])
        failed = int(suite.attrib["failures"])
        errors = int(suite.attrib["errors"])
        skipped = int(suite.attrib["skipped"])
    except (KeyError, ValueError) as exc:
        raise ValueError("junit testsuite is missing integer outcome counts") from exc

    files: set[str] = set()
    failure_nodes: list[str] = []
    error_nodes: list[str] = []
    cases = tuple(suite.iter("testcase"))
    if len(cases) != collected:
        raise ValueError(f"junit testcase count mismatch: {len(cases)} != {collected}")
    for testcase in cases:
        source, node_id = _node_id(testcase, repo_root)
        files.add(source)
        if testcase.find("failure") is not None:
            failure_nodes.append(node_id)
        if testcase.find("error") is not None:
            error_nodes.append(node_id)
    if len(failure_nodes) != failed or len(error_nodes) != errors:
        raise ValueError("junit failure/error elements do not match testsuite counts")
    passed = collected - failed - errors - skipped
    if passed < 0:
        raise ValueError("junit outcome counts exceed collected tests")
    return RegressionSummary(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        test_files=tuple(sorted(files)),
        failure_node_ids=tuple(sorted(failure_nodes)),
        error_node_ids=tuple(sorted(error_nodes)),
    )


def compare_summary(actual: RegressionSummary, baseline: Baseline) -> RegressionVerdict:
    count_mismatches = tuple(
        f"{name}: expected={getattr(baseline, name)} actual={getattr(actual, name)}"
        for name in ("collected", "passed", "failed", "errors", "skipped")
        if getattr(actual, name) != getattr(baseline, name)
    )
    expected_failures = set(baseline.failure_node_ids)
    actual_failures = set(actual.failure_node_ids)
    expected_errors = set(baseline.error_node_ids)
    actual_errors = set(actual.error_node_ids)
    missing_failures = tuple(sorted(expected_failures - actual_failures))
    unexpected_failures = tuple(sorted(actual_failures - expected_failures))
    missing_errors = tuple(sorted(expected_errors - actual_errors))
    unexpected_errors = tuple(sorted(actual_errors - expected_errors))
    ok = not any(
        (
            count_mismatches,
            missing_failures,
            unexpected_failures,
            missing_errors,
            unexpected_errors,
        )
    )
    return RegressionVerdict(
        ok=ok,
        count_mismatches=count_mismatches,
        missing_failures=missing_failures,
        unexpected_failures=unexpected_failures,
        missing_errors=missing_errors,
        unexpected_errors=unexpected_errors,
    )


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _require_clean_worktree() -> str:
    status = _git_output("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(f"canonical regression requires a clean worktree:\n{status}")
    return _git_output("rev-parse", "HEAD")


def run_regression(*, baseline_path: Path, output_dir: Path) -> int:
    head = _require_clean_worktree()
    baseline = Baseline.load(baseline_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    junit_path = output_dir / "junit.xml"
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:randomly",
        "tests",
        f"--junitxml={junit_path}",
    )
    env = os.environ.copy()
    pythonpath = f"{ROOT / 'pipeline/scripts/ingest_hook'}:{ROOT}"
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}:{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    (output_dir / "pytest_stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output_dir / "pytest_stderr.txt").write_text(result.stderr, encoding="utf-8")
    summary = parse_junit(junit_path)
    verdict = compare_summary(summary, baseline)
    payload = {
        "schema_version": 1,
        "worktree": str(ROOT),
        "head": head,
        "command": list(command),
        "pytest_returncode": result.returncode,
        "summary": asdict(summary),
        "baseline": asdict(baseline),
        "verdict": asdict(verdict),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (output_dir / "verdict.json").write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if verdict.ok else 1


def _default_output_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"jw-market-canonical-regression-{os.getpid()}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and compare the canonical root regression suite.")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir or _default_output_dir()
    try:
        return run_regression(baseline_path=args.baseline, output_dir=output_dir)
    except (OSError, RuntimeError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"canonical regression gate failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
