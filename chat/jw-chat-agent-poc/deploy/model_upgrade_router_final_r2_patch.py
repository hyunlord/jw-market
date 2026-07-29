#!/usr/bin/env python3
"""Build, but never apply, the CAS env patch for the router/final model upgrade."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


EXPECTED_CURRENT_ENV = {
    "GENOS_SERVING_ID": "190",
    "GENOS_FINAL_SERVING_ID": "190",
    "GENOS_PLANNER_SERVING_ID": "190",
    "GENOS_DEEP_SERVING_ID": "202",
    "JW_CHAT_MODEL_FAMILY": "gemini-3-flash-preview",
}
TARGET_ENV_UPDATES = {
    "GENOS_SERVING_ID": "202",
    "GENOS_FINAL_SERVING_ID": "202",
    "JW_CHAT_ROUTER_MODEL_FAMILY": "gemini-3.1-pro-preview",
    "JW_CHAT_FINAL_MODEL_FAMILY": "gemini-3.1-pro-preview",
    "JW_CHAT_PLANNER_MODEL_FAMILY": "gemini-3-flash-preview",
}


def build_model_upgrade_patch(
    deployment: dict[str, Any],
    *,
    container_name: str,
) -> list[dict[str, Any]]:
    """Return an RFC 6902 patch guarded by the exact observed Deployment state."""

    resource_version = str(deployment.get("metadata", {}).get("resourceVersion") or "")
    if not resource_version:
        raise ValueError("deployment metadata.resourceVersion is required")

    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers")
    )
    if not isinstance(containers, list):
        raise ValueError("deployment containers must be a list")

    matching_indexes = [
        index
        for index, container in enumerate(containers)
        if isinstance(container, dict) and container.get("name") == container_name
    ]
    if len(matching_indexes) != 1:
        raise ValueError(f"expected exactly one container named {container_name!r}")
    container_index = matching_indexes[0]
    container = containers[container_index]

    env = container.get("env")
    if not isinstance(env, list):
        raise ValueError(f"container {container_name!r} must have an env list")
    observed = _literal_env_values(env)
    for name, expected in EXPECTED_CURRENT_ENV.items():
        actual = observed.get(name)
        if actual != expected:
            raise ValueError(f"{name} expected {expected!r}, observed {actual!r}")

    updated_env = [dict(item) for item in env]
    indexes = {item["name"]: index for index, item in enumerate(updated_env)}
    for name, value in TARGET_ENV_UPDATES.items():
        replacement = {"name": name, "value": value}
        if name in indexes:
            updated_env[indexes[name]] = replacement
        else:
            indexes[name] = len(updated_env)
            updated_env.append(replacement)

    env_path = f"/spec/template/spec/containers/{container_index}/env"
    return [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": resource_version,
        },
        {
            "op": "test",
            "path": f"/spec/template/spec/containers/{container_index}/name",
            "value": container_name,
        },
        {"op": "test", "path": env_path, "value": env},
        {"op": "replace", "path": env_path, "value": updated_env},
    ]


def _literal_env_values(env: list[Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in env:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("every env entry must be an object with a string name")
        name = item["name"]
        if name in values:
            raise ValueError(f"duplicate env name: {name}")
        value = item.get("value")
        if isinstance(value, str):
            values[name] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the guarded router/final model upgrade JSON patch."
    )
    parser.add_argument("deployment_json", type=Path)
    parser.add_argument("--container", default="app")
    args = parser.parse_args()

    deployment = json.loads(args.deployment_json.read_text(encoding="utf-8"))
    patch = build_model_upgrade_patch(deployment, container_name=args.container)
    print(json.dumps(patch, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
