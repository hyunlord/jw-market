from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Sequence


STRICT_LOG_PATTERN = re.compile(r"Traceback|(?:^|\s)ERROR(?:\s|:|$)|(?:^|\s)5[0-9]{2}(?:\s|$)")
IDENTITY_FIELDS = ("market", "period", "source", "measure", "level")


@dataclass(frozen=True)
class GateResult:
    gate: str
    classification: str
    checked: int
    population: int
    failures: int
    tolerance: str
    environment: str
    details: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def render(self) -> str:
        fields = (
            f"gate={self.gate}",
            f"classification={self.classification}",
            f"checked={self.checked}",
            f"population={self.population}",
            "missing=fail",
            f"tolerance={self.tolerance}",
            f"failures={self.failures}",
            f"exit_code={self.exit_code}",
            f"environment={self.environment}",
        )
        return "\n".join((*self.details, *fields))


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _canonical_sha(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _unique_by_id(items: object, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a JSON array")
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError(f"{label} entries require a string id")
        identifier = item["id"]
        if identifier in indexed:
            raise ValueError(f"duplicate {label} identity: {identifier}")
        indexed[identifier] = item
    return indexed


def check_goldens(contracts_path: Path, observations_path: Path, environment: str) -> GateResult:
    contract_document = _load_json(contracts_path)
    if not isinstance(contract_document, dict):
        raise ValueError("contracts document must be a JSON object")
    contracts = _unique_by_id(contract_document.get("contracts"), label="contract")
    observations = _unique_by_id(_load_json(observations_path), label="observation")
    required_metadata = (
        "canonical_sha256",
        "request",
        "truth_basis",
        "measured_at",
        "database",
        "runtime_provenance",
    )
    details: list[str] = []
    failures = 0
    expected_ids = set(contracts)
    observed_ids = set(observations)
    missing = sorted(expected_ids - observed_ids)
    unexpected = sorted(observed_ids - expected_ids)
    if missing:
        details.append(f"missing identities: {','.join(missing)}")
        failures += len(missing)
    if unexpected:
        details.append(f"unexpected identities: {','.join(unexpected)}")
        failures += len(unexpected)

    for identifier, contract in contracts.items():
        absent_metadata = [field for field in required_metadata if not contract.get(field)]
        if absent_metadata:
            details.append(f"{identifier}: missing contract metadata {','.join(absent_metadata)}")
            failures += 1
            continue
        observation = observations.get(identifier)
        if observation is None:
            continue
        if "payload" not in observation:
            details.append(f"{identifier}: observation payload missing")
            failures += 1
            continue
        actual = _canonical_sha(observation["payload"])
        expected = str(contract["canonical_sha256"])
        if actual != expected:
            details.append(f"{identifier}: canonical sha mismatch expected={expected} actual={actual}")
            failures += 1

    return GateResult(
        gate="api_goldens",
        classification="census",
        checked=len(observations),
        population=len(contracts),
        failures=failures,
        tolerance="exact canonical sha256",
        environment=environment,
        details=tuple(details),
    )


def _parse_pod_logs(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        pod, separator, raw_path = value.partition("=")
        if not separator or not pod or not raw_path:
            raise ValueError("--pod-log must use POD=PATH")
        if pod in result:
            raise ValueError(f"duplicate pod log: {pod}")
        result[pod] = Path(raw_path)
    return result


def check_strict_logs(expected_pods: Sequence[str], pod_log_values: Sequence[str], environment: str) -> GateResult:
    pod_logs = _parse_pod_logs(pod_log_values)
    expected = set(expected_pods)
    if len(expected) != len(expected_pods):
        raise ValueError("expected pod identities must be unique")
    provided = set(pod_logs)
    details: list[str] = []
    failures = 0
    missing = sorted(expected - provided)
    unexpected = sorted(provided - expected)
    if missing:
        details.append(f"missing pod logs: {','.join(missing)}")
        failures += len(missing)
    if unexpected:
        details.append(f"unexpected pod logs: {','.join(unexpected)}")
        failures += len(unexpected)

    for pod in sorted(expected & provided):
        path = pod_logs[pod]
        if not path.is_file():
            details.append(f"{pod}: log file missing: {path}")
            failures += 1
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if STRICT_LOG_PATTERN.search(line):
                details.append(f"{pod}:{line}")
                failures += 1

    if not expected:
        details.append("empty pod population is a failure")
        failures += 1
    return GateResult(
        gate="strict_logs",
        classification="census",
        checked=len(provided),
        population=len(expected),
        failures=failures,
        tolerance="zero strict log matches",
        environment=environment,
        details=tuple(details),
    )


def check_population(candidates_path: Path, census_path: Path, environment: str) -> GateResult:
    candidates = _load_json(candidates_path)
    census = _load_json(census_path)
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a JSON array")
    if not isinstance(census, dict) or not isinstance(census.get("population"), int):
        raise ValueError("census requires an integer population")
    if not census.get("source"):
        raise ValueError("census requires an independent source description")
    checked = len(candidates)
    population = census["population"]
    details: list[str] = []
    failures = abs(population - checked)
    if checked == 0:
        details.append("empty population is a failure")
        failures = max(failures, 1)
    if checked != population:
        details.append(f"population mismatch: got {checked}, expected {population}")
    return GateResult(
        gate="population",
        classification="census",
        checked=checked,
        population=population,
        failures=failures,
        tolerance="exact identity count from independent census",
        environment=environment,
        details=tuple(details),
    )


def _identity(item: dict[str, Any]) -> tuple[str, ...]:
    missing = [field for field in IDENTITY_FIELDS if field not in item]
    if missing:
        raise ValueError(f"segment identity missing fields: {','.join(missing)}")
    return tuple(str(item[field]) for field in IDENTITY_FIELDS)


def check_segment_sums(
    expected_path: Path,
    observations_path: Path,
    abs_tol: float,
    environment: str,
) -> GateResult:
    expected_document = _load_json(expected_path)
    observations_document = _load_json(observations_path)
    if not isinstance(expected_document, dict):
        raise ValueError("expected identities document must be an object")
    classification = expected_document.get("classification")
    if classification not in {"census", "sample"}:
        raise ValueError("classification must be census or sample")
    expected_items = expected_document.get("identities")
    if not isinstance(expected_items, list) or not isinstance(observations_document, list):
        raise ValueError("segment identities and observations must be arrays")
    expected = {_identity(item) for item in expected_items if isinstance(item, dict)}
    observed: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in observations_document:
        if not isinstance(item, dict):
            raise ValueError("segment observations must be objects")
        key = _identity(item)
        if key in observed:
            raise ValueError(f"duplicate segment identity: {'|'.join(key)}")
        observed[key] = item

    details: list[str] = []
    failures = 0
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing:
        details.append("missing identities: " + ",".join("|".join(item) for item in missing))
        failures += len(missing)
    if unexpected:
        details.append("unexpected identities: " + ",".join("|".join(item) for item in unexpected))
        failures += len(unexpected)
    for key in sorted(expected & set(observed)):
        item = observed[key]
        try:
            segment_sum = float(item["segment_sum"])
            market_total = float(item["market_total"])
        except (KeyError, TypeError, ValueError) as exc:
            details.append(f"{'|'.join(key)}: invalid numeric observation: {exc}")
            failures += 1
            continue
        difference = abs(segment_sum - market_total)
        if difference > abs_tol:
            details.append(
                f"{'|'.join(key)}: sum mismatch segment_sum={segment_sum} "
                f"market_total={market_total} difference={difference}"
            )
            failures += 1

    if not expected:
        details.append("empty expected segment population is a failure")
        failures += 1
    return GateResult(
        gate="segment_sum",
        classification=classification,
        checked=len(observed),
        population=len(expected),
        failures=failures,
        tolerance=f"abs_tol={abs_tol},rel_tol=0",
        environment=environment,
        details=tuple(details),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed release acceptance gates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    goldens = subparsers.add_parser("goldens")
    goldens.add_argument("--contracts", type=Path, required=True)
    goldens.add_argument("--observations", type=Path, required=True)
    goldens.add_argument("--environment", default="local")

    logs = subparsers.add_parser("strict-logs")
    logs.add_argument("--expected-pod", action="append", default=[])
    logs.add_argument("--pod-log", action="append", default=[])
    logs.add_argument("--environment", default="local")

    population = subparsers.add_parser("population")
    population.add_argument("--candidates", type=Path, required=True)
    population.add_argument("--census", type=Path, required=True)
    population.add_argument("--environment", default="local")

    segment = subparsers.add_parser("segment-sum")
    segment.add_argument("--expected-identities", type=Path, required=True)
    segment.add_argument("--observations", type=Path, required=True)
    segment.add_argument("--abs-tol", type=float, default=0.01)
    segment.add_argument("--environment", default="local")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers: dict[str, Callable[[], GateResult]] = {
        "goldens": lambda: check_goldens(args.contracts, args.observations, args.environment),
        "strict-logs": lambda: check_strict_logs(args.expected_pod, args.pod_log, args.environment),
        "population": lambda: check_population(args.candidates, args.census, args.environment),
        "segment-sum": lambda: check_segment_sums(
            args.expected_identities,
            args.observations,
            args.abs_tol,
            args.environment,
        ),
    }
    try:
        result = handlers[args.command]()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = GateResult(
            gate=args.command.replace("-", "_"),
            classification="census",
            checked=0,
            population=0,
            failures=1,
            tolerance="not evaluated",
            environment=getattr(args, "environment", "local"),
            details=(f"gate input error: {exc}",),
        )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
