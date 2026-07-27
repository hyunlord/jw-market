from __future__ import annotations

import copy

import pytest

from pipeline.scripts.ingest_hook.render_shadow_revert_patch import (
    SHADOW_ENV,
    STAGING_ROOT,
    render_plan,
)


def _deployment() -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "jw-ingest-hook", "namespace": "llmops"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "istio-proxy", "image": "proxy@sha256:" + "1" * 64},
                        {
                            "name": "trigger",
                            "image": "orchestrator@sha256:" + "2" * 64,
                            "env": [
                                {"name": "UNRELATED", "value": "kept"},
                                *[
                                    {"name": name, "value": value}
                                    for name, value in SHADOW_ENV.items()
                                ],
                            ],
                        },
                    ]
                }
            }
        },
    }


def _apply_env_operations(deployment: dict, plan: dict) -> dict:
    result = copy.deepcopy(deployment)
    containers = result["spec"]["template"]["spec"]["containers"]
    for operation in plan["patch"]:
        parts = operation["path"].strip("/").split("/")
        if operation["op"] == "test":
            if parts[-1] == "name":
                assert containers[int(parts[-2])]["name"] == operation["value"]
            elif parts[-1] == "image":
                assert containers[int(parts[-2])]["image"] == operation["value"]
            else:
                assert containers[int(parts[-3])]["env"][int(parts[-1])] == operation["value"]
            continue
        container = containers[int(parts[4])]
        env = container["env"]
        if operation["op"] == "remove":
            env.pop(int(parts[-1]))
        elif operation["op"] == "replace":
            env[int(parts[-1])] = operation["value"]
        elif operation["op"] == "add":
            env.append(operation["value"])
        else:
            raise AssertionError(operation)
    return result


def _env_by_name(deployment: dict) -> dict[str, dict]:
    trigger = next(
        row
        for row in deployment["spec"]["template"]["spec"]["containers"]
        if row["name"] == "trigger"
    )
    return {row["name"]: row for row in trigger["env"]}


def test_to_d2_resolves_container_by_name_and_keeps_counterpart() -> None:
    source = _deployment()

    plan = render_plan(source)
    rendered = _apply_env_operations(source, plan)
    env = _env_by_name(rendered)

    assert plan["container_index"] == 1
    assert plan["image_changed"] is False
    assert plan["image_before"] == plan["image_after"]
    assert plan["patch"][0]["value"] == "trigger"
    assert plan["patch"][1]["path"].endswith("/image")
    assert env[STAGING_ROOT[0]]["value"] == STAGING_ROOT[1]
    assert env["INGEST_SHADOW_LEDGER_SQLITE"]["value"] == SHADOW_ENV[
        "INGEST_SHADOW_LEDGER_SQLITE"
    ]
    assert set(env).isdisjoint(set(SHADOW_ENV) - {"INGEST_SHADOW_LEDGER_SQLITE"})
    assert env["UNRELATED"]["value"] == "kept"


def test_to_d2_can_remove_counterpart_without_reporting_404_semantics() -> None:
    source = _deployment()

    plan = render_plan(source, retain_counterpart=False)
    rendered = _apply_env_operations(source, plan)

    assert set(_env_by_name(rendered)).isdisjoint(SHADOW_ENV)


def test_rollback_restores_all_six_shadow_values_and_removes_staging() -> None:
    reverted = _apply_env_operations(_deployment(), render_plan(_deployment()))

    plan = render_plan(reverted, direction="rollback")
    restored = _apply_env_operations(reverted, plan)
    env = _env_by_name(restored)

    assert STAGING_ROOT[0] not in env
    assert {name: env[name]["value"] for name in SHADOW_ENV} == SHADOW_ENV
    assert plan["image_changed"] is False


@pytest.mark.parametrize("forbidden", ["INGEST_LOAD_TARGET_ROOT", "INGEST_MART_PROMOTION_APPROVED"])
def test_renderer_refuses_serving_write_switches(forbidden: str) -> None:
    source = _deployment()
    trigger = source["spec"]["template"]["spec"]["containers"][1]
    trigger["env"].append({"name": forbidden, "value": "enabled"})

    with pytest.raises(ValueError, match="forbidden serving env"):
        render_plan(source)


def test_renderer_rejects_duplicate_env_names() -> None:
    source = _deployment()
    trigger = source["spec"]["template"]["spec"]["containers"][1]
    trigger["env"].append({"name": "INGEST_LOAD_SHADOW_ROOT", "value": "duplicate"})

    with pytest.raises(ValueError, match="duplicate env name"):
        render_plan(source)
