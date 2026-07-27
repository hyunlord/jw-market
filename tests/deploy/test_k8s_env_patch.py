"""Name-based container/env patch targeting, against the live 2026-07-27 layout.

The fixture is the env array actually observed on deploy/jw-ingest-hook that day:

    0 INGEST_INPUT_BACKEND   5 MARIADB_USER      (valueFrom)   7 APP_VERSION
    1 INGEST_INPUT_ROOT      6 MARIADB_PASSWORD  (valueFrom)   8 INGEST_JOB_IMAGE
    2 MARIADB_HOST
    3 MARIADB_PORT
    4 MARIADB_DATABASE

A prepared rollout script hardcoded 5 and 6 as APP_VERSION and INGEST_JOB_IMAGE. On this
layout those indices are the database credentials. test_red_* reproduces what that would
have done; everything else pins the behaviour that prevents it.

Injections ①-⑤ from the brief map to:
  ① reordered env      -> test_resolves_after_reorder
  ② name absent        -> test_absent_env_refuses_and_does_not_add
  ③ name duplicated    -> test_duplicate_env_refuses
  ④ valueFrom target   -> test_valuefrom_target_refused / test_valuefrom_guard_in_patch
  ⑤ test ops removed   -> test_without_test_ops_a_shifted_spec_is_silently_miswritten
"""
from __future__ import annotations

import copy

import pytest

from pipeline.scripts.deploy.k8s_env_patch import (
    PatchTargetError,
    build_patch,
    describe,
    resolve_container_index,
    resolve_env_index,
)

REG = ("asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/"
       "ar-jw-agn-stg-genos-dev-01/jw-pipeline-orchestrator")
OLD_DIGEST = "sha256:dbc90ce22ade74fb27b8153d6ef7aeccff051e3f2ae6fd4c9063e22592e06cb4"
NEW_DIGEST = "sha256:8f5375579b8d2176de80012a8eef498e7cb298d0d9c06349595efff11c44365f"
OLD_APP = "d53911b902b1b9c8219890b20b115aeaa3665c8d"
NEW_APP = "5e6f1629511710279fd2f5dc89d1052b877a52da"


def live_deployment() -> dict:
    """deploy/jw-ingest-hook as observed on 2026-07-27, env order verbatim."""
    return {
        "metadata": {"name": "jw-ingest-hook", "resourceVersion": "418899321"},
        "spec": {"template": {"spec": {"containers": [{
            "name": "trigger",
            "image": f"{REG}@{OLD_DIGEST}",
            "env": [
                {"name": "INGEST_INPUT_BACKEND", "value": "local"},
                {"name": "INGEST_INPUT_ROOT", "value": "/nfs-root/autoIngestion"},
                {"name": "MARIADB_HOST", "value": "llmops-mariadb-service.llmops.svc.cluster.local"},
                {"name": "MARIADB_PORT", "value": "3306"},
                {"name": "MARIADB_DATABASE", "value": "jw_mart_d2_stage_20260630_r2"},
                {"name": "MARIADB_USER",
                 "valueFrom": {"secretKeyRef": {"name": "jw-mart-d2-writer", "key": "username"}}},
                {"name": "MARIADB_PASSWORD",
                 "valueFrom": {"secretKeyRef": {"name": "jw-mart-d2-writer", "key": "password"}}},
                {"name": "APP_VERSION", "value": OLD_APP},
                {"name": "INGEST_JOB_IMAGE", "value": f"{REG}@{OLD_DIGEST}"},
                {"name": "INGEST_LOAD_SHADOW_ROOT", "value": "/market-output/shadow"},
                {"name": "INGEST_SHADOW_LEDGER_SQLITE", "value": "/market-output/shadow/ledger.sqlite"},
            ],
        }]}}},
    }


# -- a minimal JSON Patch evaluator, so the patch can be proven without a cluster ------


class PatchRejected(RuntimeError):
    pass


def _get(doc, path: str):
    node = doc
    for token in [t for t in path.split("/") if t != ""]:
        if isinstance(node, list):
            idx = int(token)
            if idx >= len(node):
                raise PatchRejected(f"path not found: {path}")
            node = node[idx]
        else:
            if token not in node:
                raise PatchRejected(f"path not found: {path}")
            node = node[token]
    return node


def apply_patch(doc: dict, ops: list[dict]) -> dict:
    """RFC 6902 subset (test/replace) with atomic semantics: all or nothing."""
    draft = copy.deepcopy(doc)
    for op in ops:
        if op["op"] == "test":
            observed = _get(draft, op["path"])          # raises if absent
            if observed != op["value"]:
                raise PatchRejected(
                    f"test failed at {op['path']}: expected {op['value']!r}, found {observed!r}"
                )
        elif op["op"] == "replace":
            parent_path, _, leaf = op["path"].rpartition("/")
            parent = _get(draft, parent_path)
            if isinstance(parent, list):
                parent[int(leaf)] = op["value"]
            else:
                if leaf not in parent:
                    raise PatchRejected(f"replace on absent path: {op['path']}")
                parent[leaf] = op["value"]
        else:
            raise PatchRejected(f"unsupported op {op['op']!r}")
    return draft


def env_map(doc: dict) -> dict:
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in container["env"]}


# -- RED: what the hardcoded indices would have done -----------------------------------


HARDCODED_PATCH = [
    {"op": "replace", "path": "/spec/template/spec/containers/0/image",
     "value": f"{REG}@{NEW_DIGEST}"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/env/6/value",
     "value": f"{REG}@{NEW_DIGEST}"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/env/5/value",
     "value": NEW_APP},
]


def test_red_1_hardcoded_patch_misses_its_targets_on_the_observed_layout():
    """Indices 5/6 are the credentials, not the targets. Two things follow.

    First, the intended targets are not touched at all — whatever else happens, the
    deploy does not do what it was written to do.

    Second, indices 5/6 hold valueFrom entries, so ``/value`` does not exist there.
    RFC 6902 §4.3 requires a replace target to exist, so a conforming applier rejects
    the patch. Whether kubectl's applier rejects or creates the member is NOT asserted
    here: confirming it would mean issuing a real ``kubectl patch``, which is out of
    bounds for this round. Both outcomes are wrong; only one of them is loud.
    """
    doc = live_deployment()
    ci, env = 0, doc["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env[5]["name"] == "MARIADB_USER" and "value" not in env[5]
    assert env[6]["name"] == "MARIADB_PASSWORD" and "value" not in env[6]

    # a conforming (strict) applier rejects
    with pytest.raises(PatchRejected, match="replace on absent path"):
        apply_patch(doc, HARDCODED_PATCH)

    # and the intended targets were never addressed by that patch
    addressed = {op["path"] for op in HARDCODED_PATCH}
    assert f"/spec/template/spec/containers/{ci}/env/7/value" not in addressed  # APP_VERSION
    assert f"/spec/template/spec/containers/{ci}/env/8/value" not in addressed  # INGEST_JOB_IMAGE


def test_red_2_hardcoded_patch_silently_corrupts_a_plausible_reordered_layout():
    """The assumption-free hazard: when 5/6 hold literals, the replace simply succeeds.

    No claim about absent-path behaviour is needed here. Move the two secret refs to the
    front — an ordinary manifest tidy-up — and indices 5/6 become MARIADB_PORT and
    MARIADB_DATABASE. The patch applies cleanly, reports success, and leaves the hook
    unable to reach its database.
    """
    doc = live_deployment()
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    secrets = [e for e in env if "valueFrom" in e]
    rest = [e for e in env if "valueFrom" not in e]
    doc["spec"]["template"]["spec"]["containers"][0]["env"] = secrets + rest

    reordered = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    assert reordered[5]["name"] == "MARIADB_PORT"
    assert reordered[6]["name"] == "MARIADB_DATABASE"

    result = apply_patch(doc, HARDCODED_PATCH)          # no exception: it just works
    after = env_map(result)
    assert after["MARIADB_PORT"]["value"] == NEW_APP
    assert after["MARIADB_DATABASE"]["value"] == f"{REG}@{NEW_DIGEST}"
    # intended targets untouched
    assert after["APP_VERSION"]["value"] == OLD_APP
    assert after["INGEST_JOB_IMAGE"]["value"] == f"{REG}@{OLD_DIGEST}"

    # the name-based builder targets correctly on this same layout
    ops = build_patch(doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
                      env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP})
    good = env_map(apply_patch(doc, ops))
    assert good["APP_VERSION"]["value"] == NEW_APP
    assert good["INGEST_JOB_IMAGE"]["value"] == f"{REG}@{NEW_DIGEST}"
    assert good["MARIADB_PORT"]["value"] == "3306"
    assert good["MARIADB_DATABASE"]["value"] == "jw_mart_d2_stage_20260630_r2"


# -- GREEN: name-based resolution on the same layout ------------------------------------


def test_resolves_the_live_layout_to_indices_7_and_8():
    doc = live_deployment()
    ci = resolve_container_index(doc, "trigger")
    assert ci == 0
    assert resolve_env_index(doc, ci, "APP_VERSION") == 7
    assert resolve_env_index(doc, ci, "INGEST_JOB_IMAGE") == 8
    # the indices the prepared script assumed are the credentials
    assert resolve_env_index(doc, ci, "MARIADB_USER") == 5
    assert resolve_env_index(doc, ci, "MARIADB_PASSWORD") == 6


def test_patch_updates_both_reference_points_and_nothing_else():
    doc = live_deployment()
    ops = build_patch(
        doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
        env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP},
    )
    result = apply_patch(doc, ops)
    after = env_map(result)
    container = result["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == f"{REG}@{NEW_DIGEST}"
    assert after["INGEST_JOB_IMAGE"]["value"] == f"{REG}@{NEW_DIGEST}"
    assert after["APP_VERSION"]["value"] == NEW_APP
    # both reference points moved together
    assert container["image"] == after["INGEST_JOB_IMAGE"]["value"]
    # credentials untouched, still references
    assert after["MARIADB_USER"] == {
        "name": "MARIADB_USER",
        "valueFrom": {"secretKeyRef": {"name": "jw-mart-d2-writer", "key": "username"}},
    }
    assert "value" not in after["MARIADB_PASSWORD"]
    # exactly three fields differ from the original
    before = live_deployment()
    diffs = _flat_diff(before, result)
    assert sorted(diffs) == [
        "/spec/template/spec/containers/0/env/7/value",
        "/spec/template/spec/containers/0/env/8/value",
        "/spec/template/spec/containers/0/image",
    ], diffs


def _flat_diff(a, b, path=""):
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
    return [k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)]


def test_patch_is_one_atomic_patch_containing_both_replaces():
    doc = live_deployment()
    ops = build_patch(
        doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
        env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP},
    )
    replaces = [o["path"] for o in ops if o["op"] == "replace"]
    assert len(replaces) == 3
    assert any(p.endswith("/image") for p in replaces)
    assert "/spec/template/spec/containers/0/env/8/value" in replaces
    # test ops precede every replace, so nothing is written before the assertions run
    first_replace = next(i for i, o in enumerate(ops) if o["op"] == "replace")
    assert all(o["op"] == "test" for o in ops[:first_replace])


# -- ① reorder -------------------------------------------------------------------------


def test_resolves_after_reorder():
    """Move the targets to different indices; resolution must follow the names."""
    doc = live_deployment()
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    env.insert(0, {"name": "NEWLY_PREPENDED", "value": "x"})
    env.append(env.pop(next(i for i, e in enumerate(env) if e["name"] == "APP_VERSION")))

    ci = resolve_container_index(doc, "trigger")
    app_idx = resolve_env_index(doc, ci, "APP_VERSION")
    job_idx = resolve_env_index(doc, ci, "INGEST_JOB_IMAGE")
    assert (app_idx, job_idx) != (7, 8)          # the hardcoded pair is no longer correct
    assert app_idx == len(env) - 1               # moved to the end
    assert env[job_idx]["name"] == "INGEST_JOB_IMAGE"

    ops = build_patch(doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
                      env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP})
    after = env_map(apply_patch(doc, ops))
    assert after["APP_VERSION"]["value"] == NEW_APP
    assert after["INGEST_JOB_IMAGE"]["value"] == f"{REG}@{NEW_DIGEST}"
    assert "value" not in after["MARIADB_USER"]


# -- ② absent --------------------------------------------------------------------------


def test_absent_env_refuses_and_does_not_add():
    doc = live_deployment()
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    del env[env.index(next(e for e in env if e["name"] == "INGEST_JOB_IMAGE"))]
    with pytest.raises(PatchTargetError, match="Refusing to add it"):
        build_patch(doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
                    env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}"})
    # nothing was appended to the spec while trying
    assert [e["name"] for e in env].count("INGEST_JOB_IMAGE") == 0


def test_absent_container_refuses():
    doc = live_deployment()
    with pytest.raises(PatchTargetError, match="not found"):
        build_patch(doc, container="istio-proxy", image="x")


# -- ③ duplicate -----------------------------------------------------------------------


def test_duplicate_env_refuses():
    doc = live_deployment()
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    env.append({"name": "INGEST_JOB_IMAGE", "value": "second-copy"})
    with pytest.raises(PatchTargetError, match="appears 2 times"):
        build_patch(doc, container="trigger",
                    env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}"})


def test_duplicate_container_refuses():
    doc = live_deployment()
    containers = doc["spec"]["template"]["spec"]["containers"]
    containers.append(dict(containers[0]))
    with pytest.raises(PatchTargetError, match="appears 2 times"):
        build_patch(doc, container="trigger", image="x")


# -- ④ valueFrom -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["MARIADB_USER", "MARIADB_PASSWORD"])
def test_valuefrom_target_refused(name):
    doc = live_deployment()
    with pytest.raises(PatchTargetError, match="valueFrom reference"):
        build_patch(doc, container="trigger", env_values={name: "anything"})


def test_valuefrom_guard_is_also_inside_the_patch():
    """Every env target carries a test on /value, which a valueFrom entry cannot satisfy."""
    doc = live_deployment()
    ops = build_patch(doc, container="trigger", env_values={"APP_VERSION": NEW_APP})
    value_tests = [o for o in ops if o["op"] == "test" and o["path"].endswith("/value")]
    assert len(value_tests) == 1
    assert value_tests[0]["value"] == OLD_APP

    # if that same index later held a valueFrom entry, the patch would be rejected
    shifted = live_deployment()
    env = shifted["spec"]["template"]["spec"]["containers"][0]["env"]
    env[7] = {"name": "APP_VERSION",
              "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}
    with pytest.raises(PatchRejected, match="path not found"):
        apply_patch(shifted, ops)


# -- ⑤ the test ops are load-bearing ---------------------------------------------------


def test_test_ops_reject_a_spec_that_moved_after_the_read():
    doc = live_deployment()
    ops = build_patch(doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
                      env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP})
    # someone else edits the Deployment between our read and our write
    moved = live_deployment()
    moved["metadata"]["resourceVersion"] = "418899999"
    moved["spec"]["template"]["spec"]["containers"][0]["env"].insert(
        0, {"name": "INJECTED_LATER", "value": "1"})
    with pytest.raises(PatchRejected, match="/metadata/resourceVersion"):
        apply_patch(moved, ops)


def test_without_test_ops_a_shifted_spec_is_silently_miswritten():
    """Strip the test ops and the same patch corrupts the wrong entries, quietly.

    This is what makes the test ops a contract rather than decoration.
    """
    doc = live_deployment()
    ops = build_patch(doc, container="trigger", image=f"{REG}@{NEW_DIGEST}",
                      env_values={"INGEST_JOB_IMAGE": f"{REG}@{NEW_DIGEST}", "APP_VERSION": NEW_APP})
    without_tests = [o for o in ops if o["op"] != "test"]
    assert len(without_tests) == 3

    # A plausible later edit: one leading variable is dropped, so everything shifts up by
    # one and indices 7/8 land on two OTHER literal entries. No absent-path assumption is
    # needed — the replace simply succeeds on the wrong variables.
    moved = live_deployment()
    container = moved["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [e for e in container["env"] if e["name"] != "INGEST_INPUT_BACKEND"]
    assert container["env"][7]["name"] == "INGEST_JOB_IMAGE"
    assert container["env"][8]["name"] == "INGEST_LOAD_SHADOW_ROOT"

    # with the test ops: rejected before anything is written
    with pytest.raises(PatchRejected):
        apply_patch(moved, ops)

    # without them: it writes, and it writes to the wrong variables, reporting success
    after = env_map(apply_patch(moved, without_tests))
    assert after["APP_VERSION"]["value"] == OLD_APP                       # target missed
    assert after["INGEST_JOB_IMAGE"]["value"] == NEW_APP                  # got a version string
    assert after["INGEST_LOAD_SHADOW_ROOT"]["value"] == f"{REG}@{NEW_DIGEST}"  # got an image ref


def test_resource_version_must_be_present():
    doc = live_deployment()
    del doc["metadata"]["resourceVersion"]
    with pytest.raises(PatchTargetError, match="resourceVersion is missing"):
        build_patch(doc, container="trigger", image="x")


def test_describe_marks_targets_and_kinds():
    text = describe(live_deployment(), "trigger", ["APP_VERSION", "INGEST_JOB_IMAGE"])
    assert "containers[0]" in text
    assert "env[5] MARIADB_USER  [valueFrom]" in text
    assert "env[8] INGEST_JOB_IMAGE  [value] <- TARGET" in text
    assert "env[7] APP_VERSION  [value] <- TARGET" in text
