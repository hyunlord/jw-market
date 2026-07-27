from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


STAGING_ROOT = ("INGEST_LOAD_STAGING_ROOT", "/tmp/ingest-load-staging")
SHADOW_ENV = {
    "INGEST_LOAD_SHADOW_ROOT": "/market-output/shadow",
    "INGEST_SHADOW_LEDGER_SQLITE": "/market-output/shadow/ledger.sqlite",
    "INGEST_SHADOW_TARGET_DB": "jw_mart_ingest_shadow_20260723",
    "INGEST_SHADOW_BUILD_PREFIX": "jw_mart_ingest_shadow_build",
    "INGEST_SHADOW_SEED_ROOT": "/market-output/ubist",
    "INGEST_SHADOW_CATALOG_ROOT": "/market-output/shadow/catalog",
}
FORBIDDEN_LIVE_ENV = (
    "INGEST_LOAD_TARGET_ROOT",
    "INGEST_MART_PROMOTION_APPROVED",
)
MANAGED_ENV = (STAGING_ROOT[0], *SHADOW_ENV)


def _named_container(deployment: dict[str, Any], name: str) -> tuple[int, dict[str, Any]]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    matches = [(index, row) for index, row in enumerate(containers) if row.get("name") == name]
    if len(matches) != 1:
        present = [row.get("name") for row in containers]
        raise ValueError(f"expected one container named {name!r}; matches={len(matches)} present={present}")
    return matches[0]


def _env_index(container: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    env = container.get("env", [])
    if not isinstance(env, list):
        raise ValueError("container env must be a list")
    indexes: dict[str, int] = {}
    for index, row in enumerate(env):
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"env[{index}] has no non-empty name")
        if name in indexes:
            raise ValueError(f"duplicate env name: {name}")
        indexes[name] = index
    return env, indexes


def _literal(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def _assert_safe_current_env(env: list[dict[str, Any]], indexes: dict[str, int]) -> None:
    active = []
    for name in FORBIDDEN_LIVE_ENV:
        index = indexes.get(name)
        if index is None:
            continue
        row = env[index]
        if row.get("value") or row.get("valueFrom"):
            active.append(name)
    if active:
        raise ValueError(f"refusing to render while forbidden serving env is active: {active}")


def _desired_env(
    *,
    direction: str,
    retain_counterpart: bool,
    env: list[dict[str, Any]],
    indexes: dict[str, int],
) -> dict[str, dict[str, Any] | None]:
    if direction == "rollback":
        return {
            STAGING_ROOT[0]: None,
            **{name: _literal(name, value) for name, value in SHADOW_ENV.items()},
        }

    desired: dict[str, dict[str, Any] | None] = {
        STAGING_ROOT[0]: _literal(*STAGING_ROOT),
        **{name: None for name in SHADOW_ENV},
    }
    sqlite_name = "INGEST_SHADOW_LEDGER_SQLITE"
    if retain_counterpart:
        sqlite_index = indexes.get(sqlite_name)
        if sqlite_index is None:
            raise ValueError(
                "counterpart retention requires the current INGEST_SHADOW_LEDGER_SQLITE value"
            )
        desired[sqlite_name] = env[sqlite_index]
    return desired


def render_plan(
    deployment: dict[str, Any],
    *,
    container_name: str = "trigger",
    direction: str = "to-d2",
    retain_counterpart: bool = True,
) -> dict[str, Any]:
    if direction not in {"to-d2", "rollback"}:
        raise ValueError(f"unsupported direction: {direction}")

    container_index, container = _named_container(deployment, container_name)
    image = container.get("image")
    if not isinstance(image, str) or not image:
        raise ValueError(f"container {container_name!r} has no immutable image coordinate")
    env, indexes = _env_index(container)
    _assert_safe_current_env(env, indexes)
    desired = _desired_env(
        direction=direction,
        retain_counterpart=retain_counterpart,
        env=env,
        indexes=indexes,
    )

    container_path = f"/spec/template/spec/containers/{container_index}"
    tests: list[dict[str, Any]] = [
        {"op": "test", "path": f"{container_path}/name", "value": container_name},
        {"op": "test", "path": f"{container_path}/image", "value": image},
    ]
    replacements: list[dict[str, Any]] = []
    removals: list[tuple[int, dict[str, Any]]] = []
    additions: list[dict[str, Any]] = []
    current_managed: dict[str, Any] = {}
    target_managed: dict[str, Any] = {}

    for name in MANAGED_ENV:
        current_index = indexes.get(name)
        current = env[current_index] if current_index is not None else None
        target = desired[name]
        current_managed[name] = current
        target_managed[name] = target
        if current == target:
            continue
        if current_index is not None:
            path = f"{container_path}/env/{current_index}"
            tests.append({"op": "test", "path": path, "value": current})
            if target is None:
                removals.append((current_index, {"op": "remove", "path": path}))
            else:
                replacements.append({"op": "replace", "path": path, "value": target})
        elif target is not None:
            additions.append({"op": "add", "path": f"{container_path}/env/-", "value": target})

    patch = [
        *tests,
        *replacements,
        *(operation for _, operation in sorted(removals, reverse=True)),
        *additions,
    ]
    changed_names = [
        name for name in MANAGED_ENV if current_managed[name] != target_managed[name]
    ]
    return {
        "schema_version": 1,
        "direction": direction,
        "counterpart_policy": "retain" if retain_counterpart else "remove",
        "container_name": container_name,
        "container_index": container_index,
        "image_before": image,
        "image_after": image,
        "image_changed": False,
        "changed_env_names": changed_names,
        "current_managed_env": current_managed,
        "target_managed_env": target_managed,
        "patch": patch,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render, but never apply, the jw-ingest-hook shadow-revert JSON patch."
    )
    parser.add_argument("deployment_json", type=Path)
    parser.add_argument("--container", default="trigger")
    parser.add_argument("--direction", choices=("to-d2", "rollback"), default="to-d2")
    parser.add_argument(
        "--remove-counterpart",
        action="store_true",
        help="Remove INGEST_SHADOW_LEDGER_SQLITE too; default retains read-only counterpart access.",
    )
    parser.add_argument(
        "--patch-only",
        action="store_true",
        help="Print only the RFC 6902 patch array for a later PL-approved kubectl patch.",
    )
    args = parser.parse_args()
    try:
        deployment = json.loads(args.deployment_json.read_text(encoding="utf-8"))
        plan = render_plan(
            deployment,
            container_name=args.container,
            direction=args.direction,
            retain_counterpart=not args.remove_counterpart,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"shadow revert patch render failed: {exc}", file=sys.stderr)
        return 2
    payload = plan["patch"] if args.patch_only else plan
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
