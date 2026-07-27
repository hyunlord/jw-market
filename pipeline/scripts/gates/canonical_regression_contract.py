from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import xml.etree.ElementTree as ET


COUNT_FIELDS = ("collected", "passed", "failed", "errors", "skipped")
BASELINE_KEYS = (
    "schema_version",
    "baseline_commit",
    "baseline_tree_digest",
    *COUNT_FIELDS,
    "collected_node_ids",
    "failure_node_ids",
    "error_node_ids",
)


@dataclass(frozen=True, slots=True)
class Baseline:
    baseline_commit: str
    baseline_tree_digest: str
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    collected_node_ids: tuple[str, ...]
    failure_node_ids: tuple[str, ...]
    error_node_ids: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Baseline:
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(set(BASELINE_KEYS) - payload.keys())
        if missing:
            raise ValueError(f"missing baseline keys: {missing}")
        if payload["schema_version"] != 2:
            raise ValueError(f"unsupported baseline schema: {payload['schema_version']}")
        commit = _validated_digest(payload["baseline_commit"], "baseline_commit")
        tree = _validated_digest(payload["baseline_tree_digest"], "baseline_tree_digest")
        collected_nodes = _validated_node_ids(payload["collected_node_ids"], "collected_node_ids")
        failure_nodes = _validated_node_ids(payload["failure_node_ids"], "failure_node_ids")
        error_nodes = _validated_node_ids(payload["error_node_ids"], "error_node_ids")
        counts = _validated_counts(payload)
        if counts["collected"] != len(collected_nodes):
            raise ValueError("baseline collected count does not match collected_node_ids")
        if counts["failed"] != len(failure_nodes):
            raise ValueError("baseline failed count does not match failure_node_ids")
        if counts["errors"] != len(error_nodes):
            raise ValueError("baseline error count does not match error_node_ids")
        if not set(failure_nodes).issubset(collected_nodes):
            raise ValueError("baseline failure_node_ids must be collected node IDs")
        if not set(error_nodes).issubset(collected_nodes):
            raise ValueError("baseline error_node_ids must be collected node IDs")
        if sum(counts[name] for name in COUNT_FIELDS[1:]) != counts["collected"]:
            raise ValueError("baseline outcome counts do not add up to collected")
        return cls(
            baseline_commit=commit,
            baseline_tree_digest=tree,
            **counts,
            collected_node_ids=collected_nodes,
            failure_node_ids=failure_nodes,
            error_node_ids=error_nodes,
        )


@dataclass(frozen=True, slots=True)
class GitMetadata:
    head_commit: str
    head_tree_digest: str
    baseline_commit_tree_digest: str
    head_descends_from_baseline: bool


@dataclass(frozen=True, slots=True)
class RegressionSummary:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    test_files: tuple[str, ...]
    collected_node_ids: tuple[str, ...]
    failure_node_ids: tuple[str, ...]
    error_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    ok: bool
    count_mismatches: tuple[str, ...]
    missing_collected: tuple[str, ...]
    unexpected_collected: tuple[str, ...]
    missing_failures: tuple[str, ...]
    unexpected_failures: tuple[str, ...]
    missing_errors: tuple[str, ...]
    unexpected_errors: tuple[str, ...]


def parse_junit(path: Path, *, repo_root: Path) -> RegressionSummary:
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
    cases = tuple(suite.iter("testcase"))
    if len(cases) != collected:
        raise ValueError(f"junit testcase count mismatch: {len(cases)} != {collected}")
    files: set[str] = set()
    collected_nodes: list[str] = []
    failure_nodes: list[str] = []
    error_nodes: list[str] = []
    for testcase in cases:
        source, node_id = _node_id(testcase, repo_root)
        files.add(source)
        collected_nodes.append(node_id)
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
        collected_node_ids=tuple(sorted(collected_nodes)),
        failure_node_ids=tuple(sorted(failure_nodes)),
        error_node_ids=tuple(sorted(error_nodes)),
    )


def compare_summary(actual: RegressionSummary, baseline: Baseline) -> RegressionVerdict:
    count_mismatches = tuple(
        f"{name}: expected={getattr(baseline, name)} actual={getattr(actual, name)}"
        for name in COUNT_FIELDS
        if getattr(actual, name) != getattr(baseline, name)
    )
    missing_collected, unexpected_collected = _node_set_delta(baseline.collected_node_ids, actual.collected_node_ids)
    missing_failures, unexpected_failures = _node_set_delta(baseline.failure_node_ids, actual.failure_node_ids)
    missing_errors, unexpected_errors = _node_set_delta(baseline.error_node_ids, actual.error_node_ids)
    verdict = RegressionVerdict(
        ok=False,
        count_mismatches=count_mismatches,
        missing_collected=missing_collected,
        unexpected_collected=unexpected_collected,
        missing_failures=missing_failures,
        unexpected_failures=unexpected_failures,
        missing_errors=missing_errors,
        unexpected_errors=unexpected_errors,
    )
    return RegressionVerdict(**{**asdict(verdict), "ok": _verdict_is_ok(verdict)})


def verify_baseline_binding(baseline: Baseline, metadata: GitMetadata) -> None:
    if metadata.baseline_commit_tree_digest != baseline.baseline_tree_digest:
        raise ValueError(
            "baseline tree digest mismatch: "
            f"expected={baseline.baseline_tree_digest} actual={metadata.baseline_commit_tree_digest}"
        )
    if not metadata.head_descends_from_baseline:
        raise ValueError(f"HEAD {metadata.head_commit} is not descended from baseline {baseline.baseline_commit}")


def _validated_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a 40-character lowercase Git digest")
    return value


def _validated_counts(payload: dict[str, object]) -> dict[str, int]:
    counts = {name: payload[name] for name in COUNT_FIELDS}
    if any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError("baseline counts must be non-negative integers")
    return counts


def _validated_node_ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a JSON array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return tuple(sorted(value))


def _node_id(testcase: ET.Element, repo_root: Path) -> tuple[str, str]:
    classname = testcase.get("classname")
    name = testcase.get("name")
    if not classname or not name:
        raise ValueError("testcase requires classname and name")
    source, classes = _test_source_and_class(classname, repo_root)
    suffix = "::".join((*classes, name))
    return source, f"{source}::{suffix}"


def _test_source_and_class(classname: str, repo_root: Path) -> tuple[str, tuple[str, ...]]:
    parts = classname.split(".")
    for prefix_length in range(len(parts), 0, -1):
        candidate = repo_root.joinpath(*parts[:prefix_length]).with_suffix(".py")
        if candidate.is_file():
            return candidate.relative_to(repo_root).as_posix(), tuple(parts[prefix_length:])
    raise ValueError(f"cannot resolve test source for classname={classname!r}")


def _node_set_delta(expected: tuple[str, ...], actual: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected_set = set(expected)
    actual_set = set(actual)
    return tuple(sorted(expected_set - actual_set)), tuple(sorted(actual_set - expected_set))


def _verdict_is_ok(verdict: RegressionVerdict) -> bool:
    return not any(value for key, value in asdict(verdict).items() if key != "ok")
