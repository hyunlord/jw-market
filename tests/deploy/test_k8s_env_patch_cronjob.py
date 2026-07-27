"""CronJob targets: the pod template is two levels deeper, and that must come from `kind`.

Fixture is the live `cronjob/brand-activity-row-topic-monthly` as observed 2026-07-27:
one container `row-topic-monthly`, image `jw-market-backend-api@sha256:e8205158…`, nine env
entries of which `DB_PASSWORD` and `JOB_NAME` are valueFrom, and NO env carrying an image
reference. So this workload has exactly ONE image reference point, unlike jw-ingest-hook
which has two.

The point of these tests: a Deployment-shaped path applied to a CronJob does not address
anything, and defaulting to it would be the same class of mistake as hardcoding an index.
"""
from __future__ import annotations

import pytest

from pipeline.scripts.deploy.k8s_env_patch import (
    PatchTargetError,
    build_patch,
    container_path,
    describe,
    resolve_container_index,
    template_path,
)
from test_k8s_env_patch import PatchRejected, apply_patch  # the shared local applier

API_REPO = ("asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/"
            "ar-jw-agn-stg-genos-dev-01/jw-market-backend-api")
OLD = "sha256:e8205158a93cf4cc34abdee2b35a8a3c3d96a4f2fb887e823a58d5a68cb9368a"
NEW = "sha256:" + "a" * 64  # stand-in; the real digest is fixed at push time


def live_cronjob() -> dict:
    return {
        "kind": "CronJob",
        "metadata": {"name": "brand-activity-row-topic-monthly", "resourceVersion": "419001234"},
        "spec": {
            "schedule": "0 22 4 * *",
            "suspend": False,
            "concurrencyPolicy": "Forbid",
            "jobTemplate": {"spec": {"template": {"spec": {
                "containers": [{
                    "name": "row-topic-monthly",
                    "image": f"{API_REPO}@{OLD}",
                    "command": ["python"],
                    "args": ["/opt/row-topic/row_topic_monthly_wrapper.py"],
                    "env": [
                        {"name": "PROJECT_ROOT", "value": "/app"},
                        {"name": "DB_HOST", "value": "llmops-mariadb-service.llmops.svc.cluster.local"},
                        {"name": "DB_PORT", "value": "3306"},
                        {"name": "DB_USER", "value": "llmops"},
                        {"name": "DB_PASSWORD",
                         "valueFrom": {"secretKeyRef": {"name": "galera-mariadb-galera", "key": "mariadb-password"}}},
                        {"name": "ROW_TOPIC_SCHEMA", "value": "jw_brand_activity_stage"},
                        {"name": "ROW_TOPIC_MAX_CALLS", "value": "350"},
                        {"name": "ROW_TOPIC_GATE_MAX_CALLS", "value": "5"},
                        {"name": "JOB_NAME",
                         "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['job-name']"}}},
                    ],
                }],
            }}}},
        },
    }


def image_of(doc: dict) -> str:
    return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]


# -- the path must come from kind ------------------------------------------------------


def test_template_path_is_derived_from_kind():
    assert template_path(live_cronjob()) == "/spec/jobTemplate/spec/template"
    assert template_path({"kind": "Deployment"}) == "/spec/template"
    assert container_path(live_cronjob(), 0) == "/spec/jobTemplate/spec/template/spec/containers/0"


def test_missing_kind_refuses_rather_than_assuming_deployment():
    doc = live_cronjob()
    del doc["kind"]
    with pytest.raises(PatchTargetError, match="has no 'kind'"):
        build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")


def test_unknown_kind_refuses():
    doc = live_cronjob()
    doc["kind"] = "Rollout"
    with pytest.raises(PatchTargetError, match="unsupported kind 'Rollout'"):
        build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")


def test_a_deployment_shaped_path_would_address_nothing_on_a_cronjob():
    """Why the derivation matters: the old constant does not exist in this object."""
    doc = live_cronjob()
    with pytest.raises(PatchRejected, match="path not found"):
        apply_patch(doc, [{"op": "test", "path": "/spec/template/spec/containers/0/name",
                           "value": "row-topic-monthly"}])


# -- the single image reference point --------------------------------------------------


def test_resolves_the_single_container_by_name():
    doc = live_cronjob()
    assert resolve_container_index(doc, "row-topic-monthly") == 0
    with pytest.raises(PatchTargetError, match="not found"):
        resolve_container_index(doc, "trigger")


def test_image_only_patch_updates_exactly_the_image():
    doc = live_cronjob()
    ops = build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")
    result = apply_patch(doc, ops)
    assert image_of(result) == f"{API_REPO}@{NEW}"

    diffs = _flat_diff(live_cronjob(), result)
    assert diffs == ["/spec/jobTemplate/spec/template/spec/containers/0/image"], diffs
    # the schedule/suspend that the 2026-08-04 firing depends on are untouched
    assert result["spec"]["schedule"] == "0 22 4 * *"
    assert result["spec"]["suspend"] is False


def test_patch_asserts_kind_specific_paths_and_current_image():
    doc = live_cronjob()
    ops = build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")
    paths = [o["path"] for o in ops]
    assert "/metadata/resourceVersion" in paths
    assert "/spec/jobTemplate/spec/template/spec/containers/0/name" in paths
    assert "/spec/jobTemplate/spec/template/spec/containers/0/image" in paths
    assert not any(p.startswith("/spec/template/") for p in paths)
    # current image is asserted, so a concurrent image change rejects the patch
    image_tests = [o for o in ops if o["op"] == "test" and o["path"].endswith("/image")]
    assert len(image_tests) == 1 and image_tests[0]["value"] == f"{API_REPO}@{OLD}"
    # test ops all precede the replace
    first_replace = next(i for i, o in enumerate(ops) if o["op"] == "replace")
    assert all(o["op"] == "test" for o in ops[:first_replace])


def test_patch_rejects_a_cronjob_that_moved_after_the_read():
    doc = live_cronjob()
    ops = build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")
    moved = live_cronjob()
    moved["metadata"]["resourceVersion"] = "419009999"
    with pytest.raises(PatchRejected, match="/metadata/resourceVersion"):
        apply_patch(moved, ops)


def test_patch_rejects_when_someone_else_already_changed_the_image():
    doc = live_cronjob()
    ops = build_patch(doc, container="row-topic-monthly", image=f"{API_REPO}@{NEW}")
    moved = live_cronjob()
    moved["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
        f"{API_REPO}@sha256:{'b' * 64}")
    with pytest.raises(PatchRejected, match="/image"):
        apply_patch(moved, ops)


# -- valueFrom protection still applies on this shape ----------------------------------


@pytest.mark.parametrize("name", ["DB_PASSWORD", "JOB_NAME"])
def test_valuefrom_env_on_a_cronjob_is_refused(name):
    doc = live_cronjob()
    with pytest.raises(PatchTargetError, match="valueFrom reference"):
        build_patch(doc, container="row-topic-monthly", env_values={name: "anything"})


def test_env_resolution_works_on_the_cronjob_shape():
    doc = live_cronjob()
    ops = build_patch(doc, container="row-topic-monthly",
                      env_values={"ROW_TOPIC_MAX_CALLS": "400"})
    result = apply_patch(doc, ops)
    env = {e["name"]: e for e in
           result["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ROW_TOPIC_MAX_CALLS"]["value"] == "400"
    assert "value" not in env["DB_PASSWORD"]
    assert all(o["path"].startswith(("/metadata", "/spec/jobTemplate")) for o in ops)


def test_describe_names_the_kind_and_the_template_path():
    text = describe(live_cronjob(), "row-topic-monthly", ["ROW_TOPIC_SCHEMA"])
    assert "CronJob template at /spec/jobTemplate/spec/template" in text
    assert "env[4] DB_PASSWORD  [valueFrom]" in text
    assert "env[5] ROW_TOPIC_SCHEMA  [value] <- TARGET" in text


def _flat_diff(a, b):
    def flat(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                yield from flat(v, f"{p}/{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                yield from flat(v, f"{p}/{i}")
        else:
            yield p, o
    fa, fb = dict(flat(a)), dict(flat(b))
    return sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
