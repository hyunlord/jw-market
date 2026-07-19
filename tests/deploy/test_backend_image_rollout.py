from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline.scripts.deploy import backend_image_rollout as rollout


OLD_IMAGE = (
    "asia-northeast3-docker.pkg.dev/example/jw-market-backend-api@sha256:"
    + "1" * 64
)
OLD_WARM_IMAGE = (
    "asia-northeast3-docker.pkg.dev/example/jw-market-backend-api@sha256:"
    + "4" * 64
)
NEW_IMAGE = (
    "asia-northeast3-docker.pkg.dev/example/jw-market-backend-api@sha256:"
    + "2" * 64
)
SOURCE_COMMIT = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def _deployment(*, image: str, generation: int) -> dict[str, object]:
    return {
        "metadata": {"generation": generation},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "jw-market-backend-api"}},
            "template": {
                "spec": {
                    "containers": [
                        {"name": "jw-market-backend-api", "image": image},
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": generation,
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "unavailableReplicas": 0,
        },
    }


def _cronjob(*, image: str) -> dict[str, object]:
    return {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "warmer", "image": image}],
                        }
                    }
                }
            }
        }
    }


def _pods(*, image: str) -> dict[str, object]:
    digest = image.split("@", maxsplit=1)[1]
    items = []
    for index in range(2):
        items.append(
            {
                "metadata": {"name": f"backend-{index}"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "jw-market-backend-api",
                            "ready": True,
                            "restartCount": 0,
                            "imageID": f"docker-pullable://example/backend@{digest}",
                        }
                    ]
                },
            }
        )
    return {"items": items}


def test_immutable_image_ref_rejects_mutable_tags() -> None:
    assert rollout.ImmutableImageRef.parse(NEW_IMAGE).digest == "sha256:" + "2" * 64

    with pytest.raises(rollout.ContractError, match="immutable digest"):
        rollout.ImmutableImageRef.parse("example/backend:latest")


def test_target_maps_backend_and_cache_warm_as_one_release_set() -> None:
    assert rollout.DeploymentTarget.for_name("prod") == rollout.DeploymentTarget(
        name="prod",
        deployment="jw-market-backend-api",
        backend_container="jw-market-backend-api",
        warm_cronjob="dynamic-market-cache-warm",
        warm_container="warmer",
    )
    assert rollout.DeploymentTarget.for_name("test2").warm_cronjob == (
        "dynamic-market-cache-warm-test2"
    )


def test_runtime_gate_uses_actual_image_ids_and_requires_full_readiness() -> None:
    target = rollout.DeploymentTarget.for_name("prod")
    state = rollout.parse_runtime_state(
        deployment_json=json.dumps(_deployment(image=NEW_IMAGE, generation=944)),
        cronjob_json=json.dumps(_cronjob(image=NEW_IMAGE)),
        pods_json=json.dumps(_pods(image=NEW_IMAGE)),
        target=target,
        expected_image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
    )

    assert state.generation == 944
    assert state.pod_names == ("backend-0", "backend-1")
    assert state.actual_image_digest == "sha256:" + "2" * 64

    bad_pods = _pods(image=NEW_IMAGE)
    bad_pods["items"][1]["status"]["containerStatuses"][0]["imageID"] = (
        "docker-pullable://example/backend@sha256:" + "3" * 64
    )
    with pytest.raises(rollout.ContractError, match="actual imageID"):
        rollout.parse_runtime_state(
            deployment_json=json.dumps(_deployment(image=NEW_IMAGE, generation=944)),
            cronjob_json=json.dumps(_cronjob(image=NEW_IMAGE)),
            pods_json=json.dumps(bad_pods),
            target=target,
            expected_image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        )


def test_runtime_gate_accepts_containerd_digest_image_ids() -> None:
    target = rollout.DeploymentTarget.for_name("prod")
    pods = _pods(image=NEW_IMAGE)
    digest = NEW_IMAGE.split("@", maxsplit=1)[1]
    for item in pods["items"]:
        item["status"]["containerStatuses"][0]["imageID"] = f"containerd://{digest}"

    state = rollout.parse_runtime_state(
        deployment_json=json.dumps(_deployment(image=NEW_IMAGE, generation=944)),
        cronjob_json=json.dumps(_cronjob(image=NEW_IMAGE)),
        pods_json=json.dumps(pods),
        target=target,
        expected_image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
    )

    assert state.actual_image_digest == digest


class FakeRunner:
    def __init__(self, *, runtime_blob: bytes, source_blob: bytes | None = None) -> None:
        self.runtime_blob = runtime_blob
        self.source_blob = source_blob if source_blob is not None else runtime_blob
        self.commands: list[tuple[str, ...]] = []
        self.backend_image = OLD_IMAGE
        self.warm_image = OLD_WARM_IMAGE
        self.generation = 943

    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> bytes:
        self.commands.append(argv)
        if argv[:4] == ("git", "rev-parse", "--verify", f"{SOURCE_COMMIT}^{{commit}}"):
            return (SOURCE_COMMIT + "\n").encode()
        if argv[:3] == ("git", "show", f"{SOURCE_COMMIT}:pipeline/scripts/api/main.py"):
            return self.source_blob
        if argv[:5] == ("kubectl", "-n", "llmops", "get", "deployment"):
            return json.dumps(_deployment(image=self.backend_image, generation=self.generation)).encode()
        if argv[:5] == ("kubectl", "-n", "llmops", "get", "cronjob"):
            return json.dumps(_cronjob(image=self.warm_image)).encode()
        if argv[:5] == ("kubectl", "-n", "llmops", "get", "pods"):
            return json.dumps(_pods(image=self.backend_image)).encode()
        if argv[:5] == ("kubectl", "-n", "llmops", "set", "image"):
            resource = argv[5]
            assignment = argv[6]
            image = assignment.split("=", maxsplit=1)[1]
            if resource.startswith("deployment/"):
                self.backend_image = image
                self.generation += 1
            else:
                self.warm_image = image
            return b"updated\n"
        if argv[:5] == ("kubectl", "-n", "llmops", "rollout", "status"):
            return b"successfully rolled out\n"
        if argv[:5] == ("kubectl", "-n", "llmops", "exec", "backend-0") or argv[:5] == (
            "kubectl",
            "-n",
            "llmops",
            "exec",
            "backend-1",
        ):
            return hashlib.sha256(self.runtime_blob).hexdigest().encode() + b"  /app/main.py\n"
        if argv[:3] == ("git", "tag", "-a"):
            return b""
        if argv[:3] == ("git", "tag", "-d"):
            return b""
        if argv[:2] == ("git", "push"):
            return b""
        raise AssertionError(f"unexpected command: {argv!r}; stdin={stdin!r}")


def test_release_updates_both_resources_then_proves_bytes_and_tags(tmp_path: Path) -> None:
    runtime_blob = b"print('exact source bytes')\n"
    runner = FakeRunner(runtime_blob=runtime_blob)
    config = rollout.RolloutConfig(
        target=rollout.DeploymentTarget.for_name("prod"),
        image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        source_commit=SOURCE_COMMIT,
        expected_generation=943,
        blob_proofs=(
            rollout.BlobProof(
                repository_path="pipeline/scripts/api/main.py",
                runtime_path="/app/main.py",
            ),
        ),
        namespace="llmops",
        git_remote="jw-private",
        freeze_marker=tmp_path / "not-frozen",
        release_date="20260719",
    )

    result = rollout.execute_rollout(config, runner)

    assert result.generation == 944
    assert result.tag == f"ops/market-944-{SOURCE_COMMIT[:8]}-20260719"
    set_commands = [command for command in runner.commands if command[3:5] == ("set", "image")]
    assert set_commands[:2] == [
        (
            "kubectl",
            "-n",
            "llmops",
            "set",
            "image",
            "deployment/jw-market-backend-api",
            f"jw-market-backend-api={NEW_IMAGE}",
        ),
        (
            "kubectl",
            "-n",
            "llmops",
            "set",
            "image",
            "cronjob/dynamic-market-cache-warm",
            f"warmer={NEW_IMAGE}",
        ),
    ]
    assert runner.commands[-2][0:3] == ("git", "tag", "-a")
    assert any(NEW_IMAGE in argument for argument in runner.commands[-2])
    assert runner.commands[-1] == ("git", "push", "jw-private", result.tag)


def test_blob_mismatch_rolls_back_backend_and_cache_warm(tmp_path: Path) -> None:
    runner = FakeRunner(runtime_blob=b"runtime differs", source_blob=b"expected source")
    config = rollout.RolloutConfig(
        target=rollout.DeploymentTarget.for_name("prod"),
        image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        source_commit=SOURCE_COMMIT,
        expected_generation=943,
        blob_proofs=(
            rollout.BlobProof(
                repository_path="pipeline/scripts/api/main.py",
                runtime_path="/app/main.py",
            ),
        ),
        namespace="llmops",
        git_remote="jw-private",
        freeze_marker=tmp_path / "not-frozen",
        release_date="20260719",
    )

    with pytest.raises(rollout.ContractError, match="blob hash"):
        rollout.execute_rollout(config, runner)

    assert runner.backend_image == OLD_IMAGE
    assert runner.warm_image == OLD_WARM_IMAGE
    assert not any(command[:3] == ("git", "tag", "-a") for command in runner.commands)


class CronJobUpdateFailRunner(FakeRunner):
    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> bytes:
        if (
            argv[:5] == ("kubectl", "-n", "llmops", "set", "image")
            and argv[5].startswith("cronjob/")
            and NEW_IMAGE in argv[6]
        ):
            self.commands.append(argv)
            raise rollout.CommandError(argv, 1, b"injected cronjob update failure")
        return super().run(argv, stdin=stdin)


def test_partial_release_update_restores_both_previous_images(tmp_path: Path) -> None:
    runner = CronJobUpdateFailRunner(runtime_blob=b"unused")
    config = rollout.RolloutConfig(
        target=rollout.DeploymentTarget.for_name("prod"),
        image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        source_commit=SOURCE_COMMIT,
        expected_generation=943,
        blob_proofs=(
            rollout.BlobProof(
                repository_path="pipeline/scripts/api/main.py",
                runtime_path="/app/main.py",
            ),
        ),
        namespace="llmops",
        git_remote="jw-private",
        freeze_marker=tmp_path / "not-frozen",
        release_date="20260719",
    )

    with pytest.raises(rollout.CommandError, match="cronjob update failure"):
        rollout.execute_rollout(config, runner)

    assert runner.backend_image == OLD_IMAGE
    assert runner.warm_image == OLD_WARM_IMAGE


class TagPushFailRunner(FakeRunner):
    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> bytes:
        if argv[:2] == ("git", "push"):
            self.commands.append(argv)
            raise rollout.CommandError(argv, 1, b"injected tag push failure")
        return super().run(argv, stdin=stdin)


def test_tag_push_failure_deletes_local_tag_and_rolls_back(tmp_path: Path) -> None:
    runner = TagPushFailRunner(runtime_blob=b"exact bytes")
    config = rollout.RolloutConfig(
        target=rollout.DeploymentTarget.for_name("prod"),
        image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        source_commit=SOURCE_COMMIT,
        expected_generation=943,
        blob_proofs=(
            rollout.BlobProof(
                repository_path="pipeline/scripts/api/main.py",
                runtime_path="/app/main.py",
            ),
        ),
        namespace="llmops",
        git_remote="jw-private",
        freeze_marker=tmp_path / "not-frozen",
        release_date="20260719",
    )

    with pytest.raises(rollout.CommandError, match="tag push failure"):
        rollout.execute_rollout(config, runner)

    assert runner.backend_image == OLD_IMAGE
    assert runner.warm_image == OLD_WARM_IMAGE
    assert any(command[:3] == ("git", "tag", "-d") for command in runner.commands)


def test_active_freeze_marker_blocks_before_any_command(tmp_path: Path) -> None:
    freeze_marker = tmp_path / "BACKEND_DEPLOY_FREEZE"
    freeze_marker.write_text("frozen\n", encoding="utf-8")
    runner = FakeRunner(runtime_blob=b"unused")
    config = rollout.RolloutConfig(
        target=rollout.DeploymentTarget.for_name("prod"),
        image=rollout.ImmutableImageRef.parse(NEW_IMAGE),
        source_commit=SOURCE_COMMIT,
        expected_generation=943,
        blob_proofs=(
            rollout.BlobProof(
                repository_path="pipeline/scripts/api/main.py",
                runtime_path="/app/main.py",
            ),
        ),
        namespace="llmops",
        git_remote="jw-private",
        freeze_marker=freeze_marker,
        release_date="20260719",
    )

    with pytest.raises(rollout.FrozenDeploymentError):
        rollout.execute_rollout(config, runner)

    assert runner.commands == []


def test_repository_freeze_marker_stays_active_until_explicit_resume() -> None:
    marker = ROOT / "deploy/k8s/jw-market/BACKEND_DEPLOY_FREEZE"

    assert rollout.DEFAULT_FREEZE_MARKER == marker
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "jw market" in text
    assert "resume" in text
    assert "backend_image_rollout" in text
