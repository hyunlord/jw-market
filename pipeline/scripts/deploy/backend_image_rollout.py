"""Deploy the market backend and cache warmer as one immutable release set."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Protocol, TypeAlias
from zoneinfo import ZoneInfo


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_PATTERN = re.compile(r"(?P<repository>[^\s@]+)@(?P<digest>sha256:[0-9a-f]{64})\Z")
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FREEZE_MARKER = REPO_ROOT / "deploy/k8s/jw-market/BACKEND_DEPLOY_FREEZE"


class RolloutError(RuntimeError):
    """Base error for a fail-closed rollout."""


class ContractError(RolloutError):
    """Raised when release input or observed runtime state violates the contract."""


class FrozenDeploymentError(RolloutError):
    """Raised while the reviewed deployment freeze marker is present."""


class CommandError(RolloutError):
    """Raised when an external command fails."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: bytes) -> None:
        command = " ".join(argv)
        detail = stderr.decode("utf-8", errors="replace").strip()
        super().__init__(f"command failed rc={returncode}: {command}: {detail}")
        self.argv = argv
        self.returncode = returncode


class RollbackError(RolloutError):
    """Raised when the release fails and the previous release set cannot be restored."""


@dataclass(frozen=True, slots=True)
class ImmutableImageRef:
    """A registry image pinned to a complete sha256 digest."""

    raw: str
    repository: str
    digest: str

    @classmethod
    def parse(cls, value: str) -> "ImmutableImageRef":
        match = _IMAGE_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ContractError("image must use an immutable digest reference: repository@sha256:<64hex>")
        return cls(raw=value.strip(), repository=match.group("repository"), digest=match.group("digest"))


@dataclass(frozen=True, slots=True)
class DeploymentTarget:
    """The backend Deployment and cache-warm CronJob released together."""

    name: str
    deployment: str
    backend_container: str
    warm_cronjob: str
    warm_container: str

    @classmethod
    def for_name(cls, name: str) -> "DeploymentTarget":
        match name:
            case "prod":
                return cls(
                    name="prod",
                    deployment="jw-market-backend-api",
                    backend_container="jw-market-backend-api",
                    warm_cronjob="dynamic-market-cache-warm",
                    warm_container="warmer",
                )
            case "test2":
                return cls(
                    name="test2",
                    deployment="jw-market-backend-api-test",
                    backend_container="jw-market-backend-api",
                    warm_cronjob="dynamic-market-cache-warm-test2",
                    warm_container="warmer",
                )
            case unsupported:
                raise ContractError(f"unsupported deployment target: {unsupported}")


@dataclass(frozen=True, slots=True)
class BlobProof:
    """A git blob path and its byte-identical path inside the backend image."""

    repository_path: str
    runtime_path: str

    @classmethod
    def parse(cls, value: str) -> "BlobProof":
        repository_path, separator, runtime_path = value.partition("=")
        if not separator or not repository_path.strip() or not runtime_path.startswith("/"):
            raise ContractError("blob proof must be REPOSITORY_PATH=/absolute/runtime/path")
        repository = Path(repository_path)
        if repository.is_absolute() or ".." in repository.parts:
            raise ContractError("blob proof repository path must be relative and must not traverse parents")
        return cls(repository_path=repository_path.strip(), runtime_path=runtime_path.strip())


@dataclass(frozen=True, slots=True)
class RolloutConfig:
    """Inputs that make a market backend release reproducible and fail closed."""

    target: DeploymentTarget
    image: ImmutableImageRef
    source_commit: str
    expected_generation: int
    blob_proofs: tuple[BlobProof, ...]
    namespace: str
    git_remote: str
    freeze_marker: Path
    release_date: str
    rollout_timeout: str = "15m"


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Post-rollout state proven from the Deployment, CronJob, and actual pods."""

    generation: int
    pod_names: tuple[str, ...]
    actual_image_digest: str


@dataclass(frozen=True, slots=True)
class RolloutResult:
    """Successful release identity persisted in Kubernetes and git."""

    generation: int
    source_commit: str
    image: str
    tag: str
    pod_names: tuple[str, ...]


class Runner(Protocol):
    """Command boundary used by the rollout and its deterministic tests."""

    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> bytes:
        """Run a command and return stdout, or raise a typed rollout error."""


@dataclass(frozen=True, slots=True)
class SubprocessRunner:
    """Production command runner."""

    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> bytes:
        completed = subprocess.run(argv, input=stdin, capture_output=True, check=False, cwd=REPO_ROOT)
        if completed.returncode != 0:
            raise CommandError(argv, completed.returncode, completed.stderr)
        return completed.stdout


def _parse_json_object(raw: str, label: str) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _sequence(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty string")
    return value


def _integer(value: JsonValue, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{label} must be an integer")
    return value


def _path(root: dict[str, JsonValue], keys: tuple[str, ...], label: str) -> JsonValue:
    current: JsonValue = root
    for key in keys:
        current = _mapping(current, label).get(key)
    return current


def _container_image(root: dict[str, JsonValue], path: tuple[str, ...], name: str, label: str) -> str:
    containers = _sequence(_path(root, path, label), f"{label}.containers")
    for value in containers:
        container = _mapping(value, f"{label}.container")
        if container.get("name") == name:
            return _text(container.get("image"), f"{label}.{name}.image")
    raise ContractError(f"{label} does not contain container {name}")


def _metadata_generation(deployment: dict[str, JsonValue]) -> int:
    return _integer(_path(deployment, ("metadata", "generation"), "deployment.metadata"), "generation")


def _selector(deployment: dict[str, JsonValue]) -> str:
    labels = _mapping(
        _path(deployment, ("spec", "selector", "matchLabels"), "deployment.selector"),
        "deployment selector labels",
    )
    pairs: list[str] = []
    for key in sorted(labels):
        value = _text(labels[key], f"deployment selector {key}")
        pairs.append(f"{key}={value}")
    if not pairs:
        raise ContractError("deployment selector must contain at least one matchLabel")
    return ",".join(pairs)


def _extract_digest(image_id: str) -> str:
    match = re.search(r"(?P<digest>sha256:[0-9a-f]{64})\Z", image_id)
    if match is None:
        raise ContractError(f"actual imageID is not digest-pinned: {image_id}")
    return match.group("digest")


def _declared_backend_image(deployment: dict[str, JsonValue], target: DeploymentTarget) -> str:
    return _container_image(
        deployment,
        ("spec", "template", "spec", "containers"),
        target.backend_container,
        "deployment",
    )


def _declared_warm_image(cronjob: dict[str, JsonValue], target: DeploymentTarget) -> str:
    return _container_image(
        cronjob,
        ("spec", "jobTemplate", "spec", "template", "spec", "containers"),
        target.warm_container,
        "cronjob",
    )


def parse_runtime_state(
    *,
    deployment_json: str,
    cronjob_json: str,
    pods_json: str,
    target: DeploymentTarget,
    expected_image: ImmutableImageRef,
    expected_warm_image: ImmutableImageRef | None = None,
) -> RuntimeState:
    """Validate rollout convergence and actual pod identities."""
    deployment = _parse_json_object(deployment_json, "deployment")
    cronjob = _parse_json_object(cronjob_json, "cronjob")
    pods = _parse_json_object(pods_json, "pods")
    warm_image = expected_warm_image if expected_warm_image is not None else expected_image

    generation = _metadata_generation(deployment)
    observed = _integer(
        _path(deployment, ("status", "observedGeneration"), "deployment.status"),
        "observedGeneration",
    )
    replicas = _integer(_path(deployment, ("status", "replicas"), "deployment.status"), "replicas")
    ready = _integer(
        _path(deployment, ("status", "readyReplicas"), "deployment.status"),
        "readyReplicas",
    )
    updated = _integer(
        _path(deployment, ("status", "updatedReplicas"), "deployment.status"),
        "updatedReplicas",
    )
    available = _integer(
        _path(deployment, ("status", "availableReplicas"), "deployment.status"),
        "availableReplicas",
    )
    unavailable_value = _path(deployment, ("status", "unavailableReplicas"), "deployment.status")
    unavailable = 0 if unavailable_value is None else _integer(unavailable_value, "unavailableReplicas")
    if observed != generation or not (replicas == ready == updated == available) or unavailable != 0:
        raise ContractError(
            "deployment is not converged: "
            f"generation={generation} observed={observed} replicas={replicas} "
            f"updated={updated} ready={ready} available={available} unavailable={unavailable}"
        )
    if _declared_backend_image(deployment, target) != expected_image.raw:
        raise ContractError("deployment spec image does not equal the requested immutable digest")
    if _declared_warm_image(cronjob, target) != warm_image.raw:
        raise ContractError("cache-warm CronJob image does not equal the release-set digest")

    pod_names: list[str] = []
    actual_digests: set[str] = set()
    for value in _sequence(pods.get("items"), "pods.items"):
        pod = _mapping(value, "pod")
        metadata = _mapping(pod.get("metadata"), "pod.metadata")
        if metadata.get("deletionTimestamp") is not None:
            continue
        name = _text(metadata.get("name"), "pod.metadata.name")
        statuses = _sequence(_path(pod, ("status", "containerStatuses"), f"pod {name}.status"), "statuses")
        matching = [
            _mapping(status, f"pod {name} container status")
            for status in statuses
            if _mapping(status, f"pod {name} container status").get("name") == target.backend_container
        ]
        if len(matching) != 1:
            raise ContractError(f"pod {name} must expose exactly one {target.backend_container} status")
        status = matching[0]
        if status.get("ready") is not True:
            raise ContractError(f"pod {name} backend container is not Ready")
        restart_count = _integer(status.get("restartCount"), f"pod {name} restartCount")
        if restart_count != 0:
            raise ContractError(f"pod {name} restartCount is {restart_count}, expected 0")
        image_id = _text(status.get("imageID"), f"pod {name} imageID")
        actual_digests.add(_extract_digest(image_id))
        pod_names.append(name)

    if len(pod_names) != replicas:
        raise ContractError(f"actual Ready pod population is {len(pod_names)}, expected {replicas}")
    if actual_digests != {expected_image.digest}:
        raise ContractError(
            f"actual imageID digest set {sorted(actual_digests)} does not equal {expected_image.digest}"
        )
    return RuntimeState(
        generation=generation,
        pod_names=tuple(sorted(pod_names)),
        actual_image_digest=expected_image.digest,
    )


def _kubectl(config: RolloutConfig, *parts: str) -> tuple[str, ...]:
    return ("kubectl", "-n", config.namespace, *parts)


def _get_json(runner: Runner, config: RolloutConfig, resource: str, name: str) -> str:
    return runner.run(_kubectl(config, "get", resource, name, "-o", "json")).decode("utf-8")


def _get_runtime_state(
    runner: Runner,
    config: RolloutConfig,
    expected_image: ImmutableImageRef,
    expected_warm_image: ImmutableImageRef | None = None,
) -> RuntimeState:
    deployment_json = _get_json(runner, config, "deployment", config.target.deployment)
    deployment = _parse_json_object(deployment_json, "deployment")
    selector = _selector(deployment)
    cronjob_json = _get_json(runner, config, "cronjob", config.target.warm_cronjob)
    pods_json = runner.run(_kubectl(config, "get", "pods", "-l", selector, "-o", "json")).decode("utf-8")
    return parse_runtime_state(
        deployment_json=deployment_json,
        cronjob_json=cronjob_json,
        pods_json=pods_json,
        target=config.target,
        expected_image=expected_image,
        expected_warm_image=expected_warm_image,
    )


def _set_release_images(runner: Runner, config: RolloutConfig, backend_image: str, warm_image: str) -> None:
    runner.run(
        _kubectl(
            config,
            "set",
            "image",
            f"deployment/{config.target.deployment}",
            f"{config.target.backend_container}={backend_image}",
        )
    )
    runner.run(
        _kubectl(
            config,
            "set",
            "image",
            f"cronjob/{config.target.warm_cronjob}",
            f"{config.target.warm_container}={warm_image}",
        )
    )


def _wait_for_rollout(runner: Runner, config: RolloutConfig) -> None:
    runner.run(
        _kubectl(
            config,
            "rollout",
            "status",
            f"deployment/{config.target.deployment}",
            f"--timeout={config.rollout_timeout}",
        )
    )


def _verify_source_commit(runner: Runner, config: RolloutConfig) -> str:
    resolved = runner.run(("git", "rev-parse", "--verify", f"{config.source_commit}^{{commit}}")).decode().strip()
    if resolved != config.source_commit:
        raise ContractError(f"source commit resolved to {resolved}, expected exact {config.source_commit}")
    return resolved


def _verify_blob_proofs(runner: Runner, config: RolloutConfig, state: RuntimeState) -> None:
    if not config.blob_proofs:
        raise ContractError("at least one git-to-runtime blob proof is required")
    for proof in config.blob_proofs:
        blob = runner.run(("git", "show", f"{config.source_commit}:{proof.repository_path}"))
        expected_hash = hashlib.sha256(blob).hexdigest()
        for pod_name in state.pod_names:
            output = runner.run(
                _kubectl(
                    config,
                    "exec",
                    pod_name,
                    "-c",
                    config.target.backend_container,
                    "--",
                    "sha256sum",
                    proof.runtime_path,
                )
            ).decode("utf-8")
            actual_hash = output.split(maxsplit=1)[0] if output.strip() else ""
            if actual_hash != expected_hash:
                raise ContractError(
                    f"blob hash mismatch for {proof.repository_path} on {pod_name}: "
                    f"expected={expected_hash} actual={actual_hash or '<empty>'}"
                )


def _tag_name(config: RolloutConfig, generation: int) -> str:
    return f"ops/market-{generation}-{config.source_commit[:8]}-{config.release_date}"


def _create_and_push_tag(runner: Runner, config: RolloutConfig, generation: int) -> str:
    tag = _tag_name(config, generation)
    message = (
        f"image: {config.image.raw}\n"
        f"target: {config.target.name}\n"
        f"generation: {generation}\n"
        f"source: {config.source_commit}"
    )
    runner.run(("git", "tag", "-a", tag, config.source_commit, "-m", message))
    try:
        runner.run(("git", "push", config.git_remote, tag))
    except RolloutError:
        runner.run(("git", "tag", "-d", tag))
        raise
    return tag


def _rollback(
    runner: Runner,
    config: RolloutConfig,
    old_backend: ImmutableImageRef,
    old_warm: ImmutableImageRef,
) -> None:
    try:
        _set_release_images(runner, config, old_backend.raw, old_warm.raw)
        _wait_for_rollout(runner, config)
        _get_runtime_state(runner, config, old_backend, old_warm)
    except RolloutError as error:
        raise RollbackError(f"failed to restore previous backend/cache-warm release set: {error}") from error


def execute_rollout(config: RolloutConfig, runner: Runner) -> RolloutResult:
    """Release one immutable digest to backend and cache warmer, or restore both."""
    if config.freeze_marker.exists():
        raise FrozenDeploymentError(
            f"backend deployment freeze is active at {config.freeze_marker}; remove it only after jw market resume"
        )
    if config.expected_generation < 1:
        raise ContractError("expected generation must be positive")
    if not re.fullmatch(r"[0-9a-f]{40}", config.source_commit):
        raise ContractError("source commit must be a full 40-character lowercase SHA")
    if not re.fullmatch(r"[0-9]{8}", config.release_date):
        raise ContractError("release date must be YYYYMMDD")
    _verify_source_commit(runner, config)

    pre_deployment_json = _get_json(runner, config, "deployment", config.target.deployment)
    pre_cronjob_json = _get_json(runner, config, "cronjob", config.target.warm_cronjob)
    pre_deployment = _parse_json_object(pre_deployment_json, "deployment")
    pre_cronjob = _parse_json_object(pre_cronjob_json, "cronjob")
    generation = _metadata_generation(pre_deployment)
    if generation != config.expected_generation:
        raise ContractError(
            f"CAS generation mismatch: live={generation} expected={config.expected_generation}"
        )
    old_backend = ImmutableImageRef.parse(_declared_backend_image(pre_deployment, config.target))
    old_warm = ImmutableImageRef.parse(_declared_warm_image(pre_cronjob, config.target))

    try:
        _set_release_images(runner, config, config.image.raw, config.image.raw)
        _wait_for_rollout(runner, config)
        state = _get_runtime_state(runner, config, config.image)
        _verify_blob_proofs(runner, config, state)
        tag = _create_and_push_tag(runner, config, state.generation)
    except RolloutError:
        _rollback(runner, config, old_backend, old_warm)
        raise

    return RolloutResult(
        generation=state.generation,
        source_commit=config.source_commit,
        image=config.image.raw,
        tag=tag,
        pod_names=state.pod_names,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy jw-market backend and dynamic-market cache warmer as one immutable release set."
    )
    parser.add_argument("--target", choices=("prod", "test2"), required=True)
    parser.add_argument("--image", required=True, help="Complete repository@sha256:<64hex> reference")
    parser.add_argument("--source-commit", required=True, help="Exact 40-character source commit")
    parser.add_argument("--expected-generation", required=True, type=int)
    parser.add_argument(
        "--verify-blob",
        action="append",
        required=True,
        help="Repeatable REPOSITORY_PATH=/absolute/runtime/path byte proof",
    )
    parser.add_argument("--namespace", default="llmops")
    parser.add_argument("--git-remote", default="jw-private")
    parser.add_argument("--release-date", default=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
    parser.add_argument("--rollout-timeout", default="15m")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI boundary for the reviewed market backend release procedure."""
    args = _parser().parse_args(argv)
    try:
        config = RolloutConfig(
            target=DeploymentTarget.for_name(args.target),
            image=ImmutableImageRef.parse(args.image),
            source_commit=args.source_commit,
            expected_generation=args.expected_generation,
            blob_proofs=tuple(BlobProof.parse(value) for value in args.verify_blob),
            namespace=args.namespace,
            git_remote=args.git_remote,
            freeze_marker=DEFAULT_FREEZE_MARKER,
            release_date=args.release_date,
            rollout_timeout=args.rollout_timeout,
        )
        result = execute_rollout(config, SubprocessRunner())
    except RolloutError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "generation": result.generation,
                "source_commit": result.source_commit,
                "image": result.image,
                "tag": result.tag,
                "pods": list(result.pod_names),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
