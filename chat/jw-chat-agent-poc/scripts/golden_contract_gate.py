from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REQUIRED_CONTRACT_FIELDS = (
    "id",
    "gate_enabled",
    "request",
    "generation_method",
    "expected",
    "truth_basis_status",
    "truth_basis",
    "measurement_context",
)
REQUIRED_GENERATION_FIELDS = ("description", "canonicalization", "environment")
REQUIRED_TRUTH_FIELDS = ("type", "evidence", "evidence_paths", "independent_of_observation")
REQUIRED_MEASUREMENT_FIELDS = (
    "measured_at",
    "database",
    "build_sha",
    "runtime_digest",
    "file_sha256",
)
FORBIDDEN_TRUTH_TYPES = {"mock_fixture", "runtime_observation", "snapshot_rehash"}
TEMPORARY_PATH_PREFIXES = ("/tmp/", "/private/tmp/")


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    classification: str
    checked: int
    population: int
    missing: str
    tolerance: str
    failures: tuple[str, ...]
    exit_code: int
    environment: str

    def as_acceptance(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failures"] = len(self.failures)
        return payload


def numeric_equal(expected: object, actual: object) -> bool:
    """Compare complete numeric values; never accept a substring token match."""
    try:
        return Decimal(str(expected)) == Decimal(str(actual))
    except (InvalidOperation, ValueError):
        return False


def validate_contracts(
    contracts: Iterable[dict[str, Any]],
    *,
    registry_path: Path,
    environment: str,
) -> GateResult:
    rows = list(contracts)
    failures: list[str] = []
    seen: set[str] = set()
    checked = 0
    for index, contract in enumerate(rows):
        identifier = str(contract.get("id") or f"row-{index}")
        checked += 1
        if identifier in seen:
            failures.append(f"{identifier}: duplicate id")
        seen.add(identifier)
        failures.extend(_validate_contract(identifier, contract))
    population = len(rows)
    if population == 0:
        failures.append("registry population is zero")
    if checked != population:
        failures.append(f"checked/population mismatch: {checked}/{population}")
    return GateResult(
        gate="golden_truth_basis",
        classification="census",
        checked=checked,
        population=population,
        missing="fail",
        tolerance="exact",
        failures=tuple(failures),
        exit_code=1 if failures else 0,
        environment=environment,
    )


def validate_registry(root: Path, *, environment: str) -> GateResult:
    registry_path = root / "registry.json"
    registry = _load_json(registry_path)
    contract_files = registry.get("contract_files")
    if not isinstance(contract_files, list) or not contract_files:
        return _registry_failure(environment, "registry contract_files population is zero")

    failures: list[str] = []
    documents: list[dict[str, Any]] = []
    listed: set[str] = set()
    for raw_name in contract_files:
        name = str(raw_name)
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
            failures.append(f"invalid registry path: {name}")
            continue
        if _is_temporary_path(name):
            failures.append(f"temporary path is forbidden: {name}")
            continue
        listed.add(name)
        path = root / name
        try:
            document = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{name}: cannot load registry: {exc}")
            continue
        contracts = document.get("contracts")
        if not isinstance(contracts, list):
            failures.append(f"{name}: contracts must be a list")
            continue
        documents.extend(contracts)

    unlisted = {
        path.name
        for path in root.glob("*.json")
        if path.name != "registry.json" and path.name not in listed
    }
    for name in sorted(unlisted):
        failures.append(f"unlisted golden document: {name}")

    result = validate_contracts(
        documents,
        registry_path=registry_path,
        environment=environment,
    )
    combined = (*failures, *result.failures)
    return GateResult(
        gate=result.gate,
        classification=result.classification,
        checked=result.checked,
        population=result.population,
        missing=result.missing,
        tolerance=result.tolerance,
        failures=combined,
        exit_code=1 if combined else 0,
        environment=result.environment,
    )


def _validate_contract(identifier: str, contract: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    missing = [field for field in REQUIRED_CONTRACT_FIELDS if field not in contract]
    if missing:
        failures.append(f"{identifier}: missing {','.join(missing)}")
        return failures
    if contract.get("gate_enabled") is not True:
        failures.append(f"{identifier}: gate_enabled must be true")
    request = contract.get("request")
    if not isinstance(request, dict) or not request.get("kind") or not request.get("exact"):
        failures.append(f"{identifier}: request must record kind and exact request")
    generation = contract.get("generation_method")
    failures.extend(_required_object(identifier, "generation_method", generation, REQUIRED_GENERATION_FIELDS))
    truth = contract.get("truth_basis")
    failures.extend(_required_object(identifier, "truth_basis", truth, REQUIRED_TRUTH_FIELDS))
    if contract.get("truth_basis_status") != "confirmed":
        failures.append(f"{identifier}: truth_basis_status must be confirmed")
    if isinstance(truth, dict):
        truth_type = str(truth.get("type") or "")
        if truth_type in FORBIDDEN_TRUTH_TYPES:
            label = "mock" if truth_type == "mock_fixture" else "self-reference"
            failures.append(f"{identifier}: {label} truth basis is forbidden ({truth_type})")
        if truth.get("independent_of_observation") is not True:
            failures.append(f"{identifier}: self-reference is forbidden")
        evidence_paths = truth.get("evidence_paths")
        if isinstance(evidence_paths, list):
            for path in evidence_paths:
                normalized = str(path).replace("\\", "/")
                if _is_temporary_path(normalized):
                    failures.append(f"{identifier}: temporary path is forbidden: {normalized}")
                if normalized.startswith("tests/") or "/tests/" in normalized:
                    failures.append(f"{identifier}: mock/test fixture cannot be truth evidence: {normalized}")
    measurement = contract.get("measurement_context")
    failures.extend(_required_object(identifier, "measurement_context", measurement, REQUIRED_MEASUREMENT_FIELDS))
    return failures


def _required_object(
    identifier: str,
    label: str,
    value: object,
    required_fields: tuple[str, ...],
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{identifier}: {label} must be an object"]
    absent = [field for field in required_fields if value.get(field) in (None, "", [])]
    return [f"{identifier}: {label} missing {','.join(absent)}"] if absent else []


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON document must be an object")
    return payload


def _is_temporary_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized.startswith(TEMPORARY_PATH_PREFIXES) or any(
        marker in normalized for marker in (" /tmp/", " /private/tmp/")
    )


def _registry_failure(environment: str, failure: str) -> GateResult:
    return GateResult(
        gate="golden_truth_basis",
        classification="census",
        checked=0,
        population=0,
        missing="fail",
        tolerance="exact",
        failures=(failure,),
        exit_code=1,
        environment=environment,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate tracked golden truth-basis contracts.")
    parser.add_argument("--golden-root", type=Path, required=True)
    parser.add_argument("--environment", default="local")
    args = parser.parse_args()
    result = validate_registry(args.golden_root, environment=args.environment)
    for key, value in result.as_acceptance().items():
        print(f"{key}={value}")
    for failure in result.failures:
        print(f"failure={failure}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
