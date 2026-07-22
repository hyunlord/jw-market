#!/usr/bin/env python3
"""Reject env ownership manifests that claim release or replica fields."""

from __future__ import annotations

import sys
from typing import Any

import yaml


def _containers(deployment: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    pod_spec = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
    )
    found: list[tuple[str, dict[str, Any]]] = []
    for collection in ("containers", "initContainers", "ephemeralContainers"):
        rows = pod_spec.get(collection, [])
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            identity = name if isinstance(name, str) and name else str(index)
            found.append((f"{collection}[name={identity}]", row))
    return found


def evaluate(deployment: dict[str, Any]) -> tuple[int, list[str], int]:
    containers = _containers(deployment)
    if not containers:
        return 1, [], 0

    violations: list[str] = []
    spec = deployment.get("spec", {})
    if isinstance(spec, dict) and "replicas" in spec:
        violations.append("spec.replicas")
    for path, container in containers:
        if "image" in container:
            violations.append(f"spec.template.spec.{path}.image")
    return (1 if violations else 0), sorted(violations), len(containers)


def _print_result(
    *, exit_code: int, violations: list[str], population: int, error: str | None = None
) -> None:
    print("gate=manifest_field_ownership")
    print("classification=census")
    print(f"checked={population}")
    print(f"population={population}")
    print("missing=fail")
    print("tolerance=exact")
    print(f"failures={len(violations) if population else 1}")
    print(f"exit_code={exit_code}")
    print("environment=pre-apply")
    if error:
        print(f"error={error}")
    if violations:
        print("forbidden_fields=" + ",".join(violations))


def main() -> int:
    try:
        deployment = yaml.safe_load(sys.stdin.read())
        if not isinstance(deployment, dict):
            raise ValueError("Deployment must be a JSON object")
    except (ValueError, yaml.YAMLError) as exc:
        _print_result(
            exit_code=1,
            violations=[],
            population=0,
            error=f"invalid_input:{type(exc).__name__}",
        )
        return 1

    exit_code, violations, population = evaluate(deployment)
    _print_result(
        exit_code=exit_code,
        violations=violations,
        population=population,
        error="empty_container_population" if population == 0 else None,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
