from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "deploy" / "mcp_standby" / "render_standby.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("render_standby", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_deployment(name: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "llmops",
            "uid": "runtime-only",
            "resourceVersion": "123",
            "ownerReferences": [{"name": "temporal-owned"}],
            "annotations": {"deployment.kubernetes.io/revision": "7"},
        },
        "spec": {
            "replicas": 0,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": f"registry/{name}:current",
                            "env": [
                                {"name": "SAFE", "value": "kept-at-runtime"},
                                {
                                    "name": "SECRET",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": "source-secret", "key": "token"}
                                    },
                                },
                            ],
                            "ports": [{"containerPort": 8080}],
                            "volumeMounts": [
                                {"name": "global-configmap", "mountPath": "/etc/pip.conf"}
                            ],
                        }
                    ],
                    "volumes": [{"name": "global-configmap", "configMap": {"name": "global"}}],
                },
            },
        },
        "status": {"readyReplicas": 0},
    }


def test_rendered_stack_has_four_independent_standbys() -> None:
    module = _load_module()
    sources = {
        mapping.source: _source_deployment(mapping.source)
        for mapping in module.STANDBY_MAPPINGS
    }
    relay_sources = {
        name: f"# {name}\nRELAY = True\n"
        for name in module.RELAY_CONFIGMAPS
    }

    objects = module.render_stack(sources, relay_sources)
    deployments = [item for item in objects if item["kind"] == "Deployment"]
    services = [item for item in objects if item["kind"] == "Service"]
    configmaps = [item for item in objects if item["kind"] == "ConfigMap"]

    assert len(deployments) == 4
    assert len(services) == 4
    assert len(configmaps) == 2
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        assert name.startswith("mcp-") and name.endswith("-standby")
        assert "code-serving" not in name
        assert deployment["spec"]["replicas"] == 1
        assert "ownerReferences" not in deployment["metadata"]
        assert "status" not in deployment
        assert deployment["spec"]["selector"]["matchLabels"] == {
            "app.kubernetes.io/name": name
        }
        assert deployment["spec"]["template"]["metadata"]["labels"] == {
            "app.kubernetes.io/name": name,
            "auth": "external",
            "jw-market/standby": "true",
        }


def test_renderer_preserves_runtime_configuration_without_serializing_it_in_repo() -> None:
    module = _load_module()
    mapping = module.STANDBY_MAPPINGS[0]
    rendered = module.render_deployment(_source_deployment(mapping.source), mapping)
    container = rendered["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == f"registry/{mapping.source}:current"
    assert {item["name"] for item in container["env"]} == {"SAFE", "SECRET"}
    assert container["env"][1]["valueFrom"]["secretKeyRef"]["name"] == "source-secret"
    assert any(
        mount["mountPath"] == "/app/src/main.py" and mount["readOnly"] is True
        for mount in container["volumeMounts"]
    )


def test_chat_wiring_is_env_only_and_keeps_external_agent_enabled() -> None:
    module = _load_module()

    command = module.chat_env_command()

    assert "kubectl -n llmops set env deployment/jw-chat-agent-poc" in command
    assert "kubectl set image" not in command
    assert "CHAT_EXTERNAL_TOOL_AGENT_ENABLED=true" in command
    assert "http://mcp-clinicaltrials-standby-svc:8080/json" in command
    assert "http://mcp-openfda-standby-svc:8080/json" in command
    assert "http://mcp-hira-standby-svc:8080/json" in command
    assert "http://mcp-nedrug-standby-svc:8080/json" in command


def test_tracked_files_do_not_contain_credential_shaped_values() -> None:
    module = _load_module()
    findings = module.scan_tracked_files(ROOT / "deploy" / "mcp_standby")
    assert findings == []


def test_json_output_is_deterministic() -> None:
    module = _load_module()
    sources = {
        mapping.source: _source_deployment(mapping.source)
        for mapping in module.STANDBY_MAPPINGS
    }
    relay_sources = {name: name for name in module.RELAY_CONFIGMAPS}

    first = json.dumps(module.render_stack(sources, relay_sources), sort_keys=True)
    second = json.dumps(module.render_stack(sources, relay_sources), sort_keys=True)

    assert first == second


def test_live_contract_detects_runtime_drift() -> None:
    module = _load_module()
    mapping = module.STANDBY_MAPPINGS[0]
    expected = module.render_deployment(_source_deployment(mapping.source), mapping)
    live = json.loads(json.dumps(expected))

    assert module.deployment_contract(expected) == module.deployment_contract(live)

    live["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/drifted:latest"
    assert module.deployment_contract(expected) != module.deployment_contract(live)
