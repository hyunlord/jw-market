#!/usr/bin/env python3
"""Fail closed unless every required env key is present in a Deployment spec."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _required_keys(arguments: argparse.Namespace) -> set[str]:
    if arguments.required_file:
        raw: Any = json.loads(arguments.required_file.read_text(encoding="utf-8"))
    elif arguments.required_json is not None:
        raw = json.loads(arguments.required_json)
    else:
        raw = [key for key in os.environ.get("REQUIRED_ENV_KEYS", "").split(",") if key]
    if not isinstance(raw, list) or any(not isinstance(key, str) for key in raw):
        raise ValueError("required keys must be a JSON string array")
    return {key for key in raw if key}


def _present_keys(deployment: dict[str, Any]) -> set[str]:
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    return {
        row["name"]
        for container in containers
        for row in container.get("env", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }


def evaluate(deployment: dict[str, Any], required: set[str]) -> tuple[int, list[str], int]:
    if not required:
        return 1, [], 0
    present = _present_keys(deployment)
    missing = sorted(required - present)
    return (1 if missing else 0), missing, len(required & present)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--required-file", type=Path)
    group.add_argument("--required-json")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        required = _required_keys(arguments)
        deployment = json.load(sys.stdin)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("gate=env_presence")
        print("classification=census")
        print("checked=0")
        print("population=0")
        print("missing=fail")
        print("tolerance=exact")
        print("failures=1")
        print("exit_code=1")
        print("environment=runtime")
        print(f"error=invalid_input:{type(exc).__name__}")
        return 1

    exit_code, missing, checked = evaluate(deployment, required)
    failures = len(missing) if required else 1
    print("gate=env_presence")
    print("classification=census")
    print(f"checked={checked}")
    print(f"population={len(required)}")
    print("missing=fail")
    print("tolerance=exact")
    print(f"failures={failures}")
    print(f"exit_code={exit_code}")
    print("environment=runtime")
    if not required:
        print("error=empty_required_population")
    elif missing:
        print("missing_keys=" + ",".join(missing))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
