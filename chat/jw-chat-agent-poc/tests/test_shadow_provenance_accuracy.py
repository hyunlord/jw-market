from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

from jw_chat_agent_poc.orchestrator import shadow_gate_runtime as runtime
from jw_chat_agent_poc.service.runtime_provenance import (
    release_identity_payload,
    version_payload,
)


ROOT = Path(__file__).parents[1]
PATCH_SCRIPT = ROOT / "deploy" / "runtime_identity_patch.py"


def _load_patch_module():
    spec = importlib.util.spec_from_file_location("runtime_identity_patch", PATCH_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment() -> dict:
    return {
        "metadata": {"resourceVersion": "123"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "registry/chat:old@sha256:" + "1" * 64,
                            "env": [
                                {"name": "SAFE", "value": "unchanged"},
                                {"name": "APP_VERSION", "value": "a" * 40},
                                {"name": "JW_CHAT_GIT_SHA", "value": "b" * 40},
                                {
                                    "name": "JW_CHAT_IMAGE_DIGEST",
                                    "value": "sha256:" + "2" * 64,
                                },
                                {
                                    "name": "IMAGE_DIGEST",
                                    "value": "sha256:" + "3" * 64,
                                },
                            ],
                        }
                    ]
                }
            }
        },
    }


def test_app_version_overrides_stale_legacy_git_sha(monkeypatch) -> None:
    deployed_sha = "af0cd74ab08c95f11516325f17f0a9f2eaf53022"
    monkeypatch.setenv("APP_VERSION", deployed_sha)
    monkeypatch.setenv(
        "JW_CHAT_GIT_SHA",
        "e88cac82cd24875f9dc97785db61bdf6b5c9a54f",
    )

    assert release_identity_payload()["git_sha"] == deployed_sha
    assert version_payload()["git_sha"] == deployed_sha


def test_legacy_git_sha_remains_a_fallback(monkeypatch) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setenv("JW_CHAT_GIT_SHA", "legacy-sha")

    assert release_identity_payload()["git_sha"] == "legacy-sha"


def test_runtime_identity_patch_updates_image_and_all_canonical_env_values() -> None:
    module = _load_patch_module()
    source = _deployment()
    original = copy.deepcopy(source)
    deployed_sha = "af0cd74ab08c95f11516325f17f0a9f2eaf53022"
    digest = "sha256:" + "8" * 64
    candidate_image = f"registry/chat:candidate@{digest}"

    patch = module.build_runtime_identity_patch(
        source,
        candidate_image=candidate_image,
        git_sha=deployed_sha,
    )

    replacements = {
        operation["path"]: operation["value"]
        for operation in patch
        if operation["op"] == "replace"
    }
    assert replacements["/spec/template/spec/containers/0/image"] == candidate_image
    assert replacements["/spec/template/spec/containers/0/env/1/value"] == deployed_sha
    assert replacements["/spec/template/spec/containers/0/env/2/value"] == deployed_sha
    assert replacements["/spec/template/spec/containers/0/env/3/value"] == digest
    assert replacements["/spec/template/spec/containers/0/env/4/value"] == digest
    assert source == original


@pytest.mark.parametrize(
    ("candidate_image", "git_sha", "message"),
    [
        ("registry/chat:mutable", "a" * 40, "immutable sha256 digest"),
        ("registry/chat@sha256:" + "8" * 64, "short", "40 lowercase hex"),
    ],
)
def test_runtime_identity_patch_rejects_non_immutable_identity(
    candidate_image: str,
    git_sha: str,
    message: str,
) -> None:
    module = _load_patch_module()

    with pytest.raises(ValueError, match=message):
        module.build_runtime_identity_patch(
            _deployment(),
            candidate_image=candidate_image,
            git_sha=git_sha,
        )


def test_runtime_identity_patch_rejects_missing_identity_env() -> None:
    module = _load_patch_module()
    deployment = _deployment()
    deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "APP_VERSION", "value": "a" * 40}
    ]

    with pytest.raises(ValueError, match="JW_CHAT_GIT_SHA count=0"):
        module.build_runtime_identity_patch(
            deployment,
            candidate_image="registry/chat@sha256:" + "8" * 64,
            git_sha="a" * 40,
        )


def test_missing_identity_is_unknown_without_blocking_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "APP_VERSION",
        "JW_CHAT_GIT_SHA",
        "GIT_SHA",
        "COMMIT_SHA",
        "JW_CHAT_IMAGE_DIGEST",
        "IMAGE_DIGEST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_write_structured_payload", payloads.append)
    answer = "byte-identical answer"

    runtime.emit_shadow_gate_observation(
        gate=runtime.ShadowGate.OPERATION_CONTRACT,
        phase="surface",
        status="PASS",
        reason="covered",
        baseline_answer=answer,
        served_answer=answer,
    )

    assert answer.encode("utf-8") == b"byte-identical answer"
    assert payloads[-1]["git_sha"] == "unknown"
    assert payloads[-1]["image_digest"] == "unknown"
    assert payloads[-1]["byte_match_baseline_served"] is True


def test_identity_lookup_exception_emits_fail_open_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_write_structured_payload", payloads.append)
    monkeypatch.setattr(
        runtime,
        "_runtime_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("identity unavailable")),
    )

    runtime.emit_shadow_gate_observation(
        gate=runtime.ShadowGate.OPERATION_CONTRACT,
        phase="surface",
        status="PASS",
        reason="covered",
        baseline_answer="same",
        served_answer="same",
    )

    assert payloads[-1]["status"] == "EVALUATOR_EXCEPTION"
    assert payloads[-1]["git_sha"] == "unknown"
    assert payloads[-1]["image_digest"] == "unknown"
    assert payloads[-1]["answer_action"] == "unchanged"
