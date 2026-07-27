"""Build a JSON Patch that updates a container image and named env values by NAME.

Why this module exists
----------------------
On 2026-07-27 a prepared rollout script hardcoded ``/env/5`` and ``/env/6`` as
APP_VERSION and INGEST_JOB_IMAGE on ``deploy/jw-ingest-hook``. The live array had
moved: 5 and 6 were ``MARIADB_USER`` and ``MARIADB_PASSWORD``, both
``valueFrom: secretKeyRef``. Executing it verbatim would have overwritten the hook's
database credentials with an image string and stopped the ingest pipeline, and the
secretKeyRef would have been replaced by a literal, making recovery awkward. The only
thing that caught it was a human re-reading the index before patching.

An index cannot express "the entry called INGEST_JOB_IMAGE". A name can. So the index
is resolved here, at build time, from the spec that is about to be patched, and the
patch then *asserts* what it resolved:

  * ``test`` on ``/metadata/resourceVersion`` — the object has not changed since it was
    read, so the indices cannot have shifted between read and write.
  * ``test`` on each target's ``name`` — the index still holds the entry we resolved.
  * ``test`` on each target's current ``value`` — this doubles as the valueFrom guard.
    A ``valueFrom`` entry has no ``/value`` member, so the test fails on a missing path
    and the whole patch is rejected before anything is written. It also rejects a
    concurrent edit of that one field.

JSON Patch is applied atomically by the API server: if any ``test`` fails, no operation
in the patch takes effect. That turns "remember to re-check the index" into a machine
check.

Refusals, all deliberate (clause 2 — not knowing is not the same as fine):
  * env name absent      -> raise. Never ``add``. A name that should be there and is not
                            is an anomaly, and inventing it hides whatever caused that.
  * env name duplicated  -> raise. A single match is not guaranteed, so a value that
                            happened to land correctly would be luck (clause 3).
  * container absent /
    duplicated           -> raise, same reasoning.
  * target has valueFrom -> raise here, in addition to the ``test`` op, so the caller
                            gets a readable reason instead of an API-server 422.

The patch is returned whole. Callers apply it in ONE ``kubectl patch --type=json``
invocation: the two-reference-point contract (container image and INGEST_JOB_IMAGE env)
is only satisfied if both move together, and a single atomic patch is what enforces that.
"""
from __future__ import annotations

# Where the pod template sits differs by kind, and the difference is not cosmetic: a
# CronJob's containers are two levels deeper. Deriving it from the object's own `kind`
# keeps the caller from having to know, and an unrecognised kind raises rather than
# defaulting to the Deployment shape — guessing the path is how you patch the wrong thing.
TEMPLATE_PATHS = {
    "Deployment": "/spec/template",
    "StatefulSet": "/spec/template",
    "DaemonSet": "/spec/template",
    "ReplicaSet": "/spec/template",
    "Job": "/spec/template",
    "CronJob": "/spec/jobTemplate/spec/template",
}

# Retained for the Deployment case so existing callers and their tests keep working.
ENV_PATH = "/spec/template/spec/containers/{ci}/env/{ei}"
CONTAINER_PATH = "/spec/template/spec/containers/{ci}"


class PatchTargetError(RuntimeError):
    """The patch cannot be built safely against the observed spec."""


def template_path(obj: dict) -> str:
    """Pod-template JSON-Pointer prefix for ``obj``, from its own ``kind``.

    Raises on a missing or unknown kind. A default would silently produce a
    Deployment-shaped path for a CronJob, and the patch would then either fail
    confusingly or — worse, if the shapes happened to line up — write somewhere else.
    """
    kind = obj.get("kind")
    if not kind:
        raise PatchTargetError(
            "object has no 'kind'; pass the object as returned by "
            "'kubectl get <kind> <name> -o json' so the template path can be derived"
        )
    if kind not in TEMPLATE_PATHS:
        raise PatchTargetError(
            f"unsupported kind {kind!r}; known kinds: {sorted(TEMPLATE_PATHS)}"
        )
    return TEMPLATE_PATHS[kind]


def container_path(obj: dict, container_index: int) -> str:
    return f"{template_path(obj)}/spec/containers/{container_index}"


def env_path(obj: dict, container_index: int, env_index: int) -> str:
    return f"{container_path(obj, container_index)}/env/{env_index}"


def _containers(deployment: dict) -> list[dict]:
    node = deployment
    try:
        for token in [t for t in template_path(deployment).split("/") if t]:
            node = node[token]
        return node["spec"]["containers"]
    except PatchTargetError:
        raise
    except (KeyError, TypeError) as exc:
        raise PatchTargetError(
            f"{deployment.get('kind')} has no container list at "
            f"{template_path(deployment)}/spec/containers: {exc}"
        ) from exc


def resolve_container_index(deployment: dict, container: str) -> int:
    """Index of ``container`` in the pod spec. Raises unless exactly one matches."""
    matches = [i for i, c in enumerate(_containers(deployment)) if c.get("name") == container]
    if not matches:
        present = [c.get("name") for c in _containers(deployment)]
        raise PatchTargetError(
            f"container {container!r} not found; present={present}"
        )
    if len(matches) > 1:
        raise PatchTargetError(
            f"container {container!r} appears {len(matches)} times at {matches}; "
            "a single match is not guaranteed"
        )
    return matches[0]


def resolve_env_index(deployment: dict, container_index: int, name: str) -> int:
    """Index of env entry ``name``. Raises on absent or duplicated — never adds."""
    env = _containers(deployment)[container_index].get("env", [])
    matches = [i for i, e in enumerate(env) if e.get("name") == name]
    if not matches:
        present = [e.get("name") for e in env]
        raise PatchTargetError(
            f"env {name!r} not found on container index {container_index}; present={present}. "
            "Refusing to add it: an expected variable that is absent is an anomaly, not "
            "something to create during a deploy"
        )
    if len(matches) > 1:
        raise PatchTargetError(
            f"env {name!r} appears {len(matches)} times at {matches}; "
            "a single match is not guaranteed, so any value written would be luck"
        )
    return matches[0]


def _literal_value(deployment: dict, container_index: int, env_index: int, name: str) -> str:
    entry = _containers(deployment)[container_index]["env"][env_index]
    if "valueFrom" in entry and "value" not in entry:
        raise PatchTargetError(
            f"env {name!r} is a valueFrom reference ({sorted(entry['valueFrom'])}), not a "
            "literal. Replacing its /value would turn a secret/config reference into a "
            "hardcoded string; refusing"
        )
    if "value" not in entry:
        raise PatchTargetError(f"env {name!r} has neither value nor valueFrom: {entry!r}")
    return entry["value"]


def build_patch(
    deployment: dict,
    *,
    container: str,
    image: str | None = None,
    env_values: dict[str, str] | None = None,
    assert_resource_version: bool = True,
) -> list[dict]:
    """Return one JSON Patch updating ``image`` and ``env_values`` by name.

    ``deployment`` is the object as just read from the API server; its
    ``metadata.resourceVersion`` is asserted so the resolved indices cannot have moved
    between the read and the write.
    """
    env_values = env_values or {}
    if image is None and not env_values:
        raise PatchTargetError("nothing to patch: pass image and/or env_values")

    ci = resolve_container_index(deployment, container)
    cpath = container_path(deployment, ci)
    ops: list[dict] = []

    if assert_resource_version:
        version = (deployment.get("metadata") or {}).get("resourceVersion")
        if not version:
            raise PatchTargetError(
                "metadata.resourceVersion is missing; cannot assert the spec did not "
                "change between read and write"
            )
        ops.append({"op": "test", "path": "/metadata/resourceVersion", "value": version})

    # Assert the container index still holds the container we resolved.
    ops.append({"op": "test", "path": cpath + "/name", "value": container})

    if image is not None:
        current = _containers(deployment)[ci].get("image")
        if current is None:
            raise PatchTargetError(f"container {container!r} has no image field")
        ops.append({"op": "test", "path": cpath + "/image", "value": current})

    # Resolve every env target BEFORE emitting any replace, so a bad target aborts the
    # whole build rather than producing a partial patch.
    resolved: list[tuple[str, int, str]] = []
    for name in sorted(env_values):
        ei = resolve_env_index(deployment, ci, name)
        resolved.append((name, ei, _literal_value(deployment, ci, ei, name)))

    for name, ei, current in resolved:
        base = env_path(deployment, ci, ei)
        # name test: the index still holds this variable.
        ops.append({"op": "test", "path": base + "/name", "value": name})
        # value test: /value exists (so this is a literal, not a valueFrom) and is
        # unchanged. This is the valueFrom guard expressed inside the patch itself.
        ops.append({"op": "test", "path": base + "/value", "value": current})

    if image is not None:
        ops.append({"op": "replace", "path": cpath + "/image", "value": image})
    for name, ei, _current in resolved:
        ops.append({
            "op": "replace",
            "path": env_path(deployment, ci, ei) + "/value",
            "value": env_values[name],
        })
    return ops


def describe(deployment: dict, container: str, names: list[str]) -> str:
    """Human-readable resolution table, for the operator to read before applying."""
    ci = resolve_container_index(deployment, container)
    env = _containers(deployment)[ci].get("env", [])
    lines = [f"{deployment.get('kind')} template at {template_path(deployment)}",
             f"container {container!r} -> containers[{ci}]  (resolved by name)"]
    for i, entry in enumerate(env):
        kind = "value" if "value" in entry else ("valueFrom" if "valueFrom" in entry else "?")
        mark = " <- TARGET" if entry.get("name") in names else ""
        lines.append(f"  env[{i}] {entry.get('name')}  [{kind}]{mark}")
    return "\n".join(lines)
