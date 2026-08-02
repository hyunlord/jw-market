from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"@(?P<digest>sha256:[0-9a-f]{64})$")
_IDENTITY_ENV_VALUES = (
    "APP_VERSION",
    "JW_CHAT_GIT_SHA",
    "JW_CHAT_IMAGE_DIGEST",
    "IMAGE_DIGEST",
)


def build_runtime_identity_patch(
    deployment: Mapping[str, Any],
    *,
    candidate_image: str,
    git_sha: str,
    container_name: str = "app",
) -> list[dict[str, Any]]:
    """Build a CAS JSON patch that keeps runtime identity aligned with the image."""

    digest_match = _IMAGE_DIGEST.search(candidate_image)
    if digest_match is None:
        raise ValueError("candidate image must end with an immutable sha256 digest")
    if _GIT_SHA.fullmatch(git_sha) is None:
        raise ValueError("git_sha must be 40 lowercase hex characters")

    metadata = deployment.get("metadata")
    if not isinstance(metadata, Mapping) or not metadata.get("resourceVersion"):
        raise ValueError("deployment metadata.resourceVersion is required")
    containers = _containers(deployment)
    container_indexes = [
        index
        for index, container in enumerate(containers)
        if isinstance(container, Mapping) and container.get("name") == container_name
    ]
    if len(container_indexes) != 1:
        raise ValueError(f"container {container_name!r} count={len(container_indexes)}")

    container_index = container_indexes[0]
    container = containers[container_index]
    env = container.get("env")
    if not isinstance(env, list):
        raise ValueError(f"container {container_name!r} env must be a list")
    env_indexes = {
        name: [
            index
            for index, item in enumerate(env)
            if isinstance(item, Mapping) and item.get("name") == name
        ]
        for name in _IDENTITY_ENV_VALUES
    }
    for name, indexes in env_indexes.items():
        if len(indexes) != 1:
            raise ValueError(f"{name} count={len(indexes)}")

    container_path = f"/spec/template/spec/containers/{container_index}"
    patch: list[dict[str, Any]] = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": metadata["resourceVersion"],
        },
        {
            "op": "test",
            "path": f"{container_path}/name",
            "value": container_name,
        },
        {
            "op": "test",
            "path": f"{container_path}/image",
            "value": container.get("image"),
        },
        {
            "op": "replace",
            "path": f"{container_path}/image",
            "value": candidate_image,
        },
    ]
    digest = digest_match.group("digest")
    replacements = {
        "APP_VERSION": git_sha,
        "JW_CHAT_GIT_SHA": git_sha,
        "JW_CHAT_IMAGE_DIGEST": digest,
        "IMAGE_DIGEST": digest,
    }
    for name in _IDENTITY_ENV_VALUES:
        env_index = env_indexes[name][0]
        env_path = f"{container_path}/env/{env_index}"
        patch.extend(
            (
                {"op": "test", "path": env_path, "value": env[env_index]},
                {
                    "op": "replace",
                    "path": f"{env_path}/value",
                    "value": replacements[name],
                },
            )
        )
    return patch


def _containers(deployment: Mapping[str, Any]) -> list[Any]:
    try:
        containers = deployment["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError) as exc:
        raise ValueError("deployment containers are required") from exc
    if not isinstance(containers, list):
        raise ValueError("deployment containers must be a list")
    return containers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Kubernetes CAS patch for chat runtime release identity."
    )
    parser.add_argument("deployment", type=Path, help="current Deployment JSON")
    parser.add_argument("candidate_image", help="immutable image reference ending in @sha256")
    parser.add_argument("git_sha", help="40-character deployed commit SHA")
    parser.add_argument("--container", default="app", help="container name (default: app)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with args.deployment.open(encoding="utf-8") as handle:
        deployment = json.load(handle)
    patch = build_runtime_identity_patch(
        deployment,
        candidate_image=args.candidate_image,
        git_sha=args.git_sha,
        container_name=args.container,
    )
    json.dump(patch, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
