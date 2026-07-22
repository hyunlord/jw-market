from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_MANIFEST = REPO_ROOT / "chat/jw-chat-agent-poc/deploy/deployment.yaml"
BRIDGE_MANIFEST = REPO_ROOT / "chat/wf301-vdb-bridge/deploy/deployment.yaml"
GATE = REPO_ROOT / "pipeline/scripts/gates/env_presence_gate.py"
REQUIRED_DIR = REPO_ROOT / "pipeline/scripts/gates/required_env"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _container(manifest: dict, name: str) -> dict:
    containers = manifest["spec"]["template"]["spec"]["containers"]
    return next(container for container in containers if container["name"] == name)


def _env_by_name(container: dict) -> dict[str, dict]:
    return {row["name"]: row for row in container.get("env", [])}


def _run_gate(manifest: dict, required: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--required-json", json.dumps(required)],
        input=json.dumps(manifest),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_complete_deployment_manifests_replace_chat_fragments() -> None:
    chat = _load_yaml(CHAT_MANIFEST)
    bridge = _load_yaml(BRIDGE_MANIFEST)

    assert chat["apiVersion"] == bridge["apiVersion"] == "apps/v1"
    assert chat["kind"] == bridge["kind"] == "Deployment"
    assert chat["metadata"]["name"] == "jw-chat-agent-poc"
    assert bridge["metadata"]["name"] == "code-serving-235"
    assert chat["spec"]["selector"]
    assert bridge["spec"]["selector"]
    assert "replicas" not in chat["spec"]
    assert bridge["spec"]["replicas"] == 1
    assert not (CHAT_MANIFEST.parent / "d2-database-env-patch.yaml").exists()
    assert not (CHAT_MANIFEST.parent / "startup-warmup-deployment-patch.yaml").exists()


def test_chat_manifest_preserves_redlines_and_secret_references() -> None:
    manifest = _load_yaml(CHAT_MANIFEST)
    container = _container(manifest, "app")
    env = _env_by_name(container)

    for key in (
        "CHAT_EXTERNAL_TOOL_AGENT_ENABLED",
        "GENERAL_VIEW_ENABLED",
        "HISTORY_PROJECTION_ENABLED",
        "CHAT_METRICS_MODE",
        "GENOS_SERVING_ID",
        "GENOS_FINAL_SERVING_ID",
        "GENOS_PLANNER_SERVING_ID",
        "GENOS_DEEP_SERVING_ID",
        "CLINICAL_TRIALS_MCP_URL",
        "OPENFDA_MCP_URL",
        "HIRA_MCP_URL",
        "NEDRUG_MCP_URL",
    ):
        assert key in env

    assert env["CHAT_CACHE_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "galera-mariadb-galera",
        "key": "mariadb-password",
    }
    assert env["DIRECT_ROUTE_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-chat-agent-direct-api-key",
        "key": "DIRECT_ROUTE_API_KEY",
    }
    assert container["envFrom"] == [{"secretRef": {"name": "jw-chat-agent-poc-secrets"}}]


def test_release_markers_and_stale_markers_are_not_hardcoded() -> None:
    chat_env = _env_by_name(_container(_load_yaml(CHAT_MANIFEST), "app"))
    bridge_env = _env_by_name(_container(_load_yaml(BRIDGE_MANIFEST), "code-serving-235"))

    assert {"JW_CHAT_GIT_SHA", "APP_VERSION", "GIT_SHA", "COMMIT_SHA"}.isdisjoint(chat_env)
    assert {"JW_235_GIT_SHA", "OPENAPI_VERSION"}.isdisjoint(bridge_env)
    assert "FILE_SQL_ENABLED" not in bridge_env


def test_235_credential_bearing_repository_url_uses_secret_reference() -> None:
    bridge_env = _env_by_name(_container(_load_yaml(BRIDGE_MANIFEST), "code-serving-235"))

    assert bridge_env["REPOSITORY_URL"] == {
        "name": "REPOSITORY_URL",
        "valueFrom": {
            "secretKeyRef": {
                "name": "code-serving-235-runtime-secrets",
                "key": "REPOSITORY_URL",
            }
        },
    }
    for entry in bridge_env.values():
        value = entry.get("value")
        assert not (
            isinstance(value, str)
            and re.search(r"https?://[^\s/:@]+:[^\s/@]+@", value)
        )


def test_required_sets_cover_release_and_redline_keys() -> None:
    chat_required = json.loads((REQUIRED_DIR / "jw-chat-agent-poc.json").read_text(encoding="utf-8"))
    bridge_required = json.loads((REQUIRED_DIR / "code-serving-235.json").read_text(encoding="utf-8"))

    assert set(
        (
            "JW_CHAT_GIT_SHA",
            "APP_VERSION",
            "CHAT_EXTERNAL_TOOL_AGENT_ENABLED",
            "CLINICAL_TRIALS_MCP_URL",
            "OPENFDA_MCP_URL",
            "HIRA_MCP_URL",
            "NEDRUG_MCP_URL",
        )
    ).issubset(chat_required)
    assert {"JW_235_GIT_SHA", "OPENAPI_VERSION", "COMMIT_ENABLED"}.issubset(bridge_required)
    assert "FILE_SQL_ENABLED" not in bridge_required
    chat_manifest_keys = set(_env_by_name(_container(_load_yaml(CHAT_MANIFEST), "app")))
    bridge_manifest_keys = set(
        _env_by_name(_container(_load_yaml(BRIDGE_MANIFEST), "code-serving-235"))
    )
    assert set(chat_required) == chat_manifest_keys | {"JW_CHAT_GIT_SHA", "APP_VERSION"}
    assert set(bridge_required) == bridge_manifest_keys | {"JW_235_GIT_SHA", "OPENAPI_VERSION"}


def test_env_presence_gate_passes_complete_fixture() -> None:
    manifest = _load_yaml(CHAT_MANIFEST)
    present = sorted(_env_by_name(_container(manifest, "app")))
    result = _run_gate(manifest, present)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "failures=0" in result.stdout
    assert f"population={len(present)}" in result.stdout


def test_env_presence_gate_fails_when_required_key_is_missing() -> None:
    manifest = _load_yaml(CHAT_MANIFEST)
    result = _run_gate(manifest, ["CHAT_METRICS_MODE", "INTENTIONALLY_MISSING"])

    assert result.returncode == 1
    assert "missing_keys=INTENTIONALLY_MISSING" in result.stdout
    assert "failures=1" in result.stdout


def test_env_presence_gate_fails_on_empty_required_population() -> None:
    manifest = _load_yaml(CHAT_MANIFEST)
    result = _run_gate(manifest, [])

    assert result.returncode == 1
    assert "error=empty_required_population" in result.stdout
    assert "population=0" in result.stdout
