# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# uv run render_standby.py apply
# uv run render_standby.py dry-run-server
# uv run render_standby.py verify-live
# uv run render_standby.py wire-chat

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple


NAMESPACE = "llmops"
CHAT_DEPLOYMENT = "jw-chat-agent-poc"


class StandbyMapping(NamedTuple):
    source: str
    target: str
    relay_configmap: str


STANDBY_MAPPINGS = (
    StandbyMapping("code-serving-112", "mcp-clinicaltrials-standby", "mcp-standby-relay-utf8-mcps"),
    StandbyMapping("code-serving-127", "mcp-openfda-standby", "mcp-standby-relay-utf8-mcps"),
    StandbyMapping("code-serving-190", "mcp-hira-standby", "mcp-standby-relay-utf8"),
    StandbyMapping("code-serving-196", "mcp-nedrug-standby", "mcp-standby-relay-utf8"),
)
RELAY_CONFIGMAPS = tuple(dict.fromkeys(mapping.relay_configmap for mapping in STANDBY_MAPPINGS))
CHAT_ENV = {
    "CLINICAL_TRIALS_MCP_URL": "http://mcp-clinicaltrials-standby-svc:8080/json",
    "OPENFDA_MCP_URL": "http://mcp-openfda-standby-svc:8080/json",
    "HIRA_MCP_URL": "http://mcp-hira-standby-svc:8080/json",
    "NEDRUG_MCP_URL": "http://mcp-nedrug-standby-svc:8080/json",
    "CHAT_EXTERNAL_TOOL_AGENT_ENABLED": "true",
}
SERVER_METADATA = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "finalizers",
    "generation",
    "managedFields",
    "ownerReferences",
    "resourceVersion",
    "selfLink",
    "uid",
}
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization": re.compile(
        rb"(?i)authorization\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]{8,}"
    ),
    "embedded_url_auth": re.compile(rb"https?://[^\s/:@]+:[^\s/@]+@"),
    "credential_assignment": re.compile(
        rb"(?i)(?:password|passwd|api[_-]?key|secret[_-]?key|access[_-]?token)"
        rb"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.=-]{8,}"
    ),
}


def _clean_metadata(metadata: dict) -> dict:
    cleaned = {
        key: copy.deepcopy(value)
        for key, value in metadata.items()
        if key not in SERVER_METADATA
    }
    annotations = cleaned.get("annotations", {})
    annotations.pop("deployment.kubernetes.io/revision", None)
    if annotations:
        cleaned["annotations"] = annotations
    else:
        cleaned.pop("annotations", None)
    return cleaned


def _replace_named_entry(entries: list[dict], replacement: dict) -> None:
    entries[:] = [entry for entry in entries if entry.get("name") != replacement["name"]]
    entries.append(replacement)


def render_deployment(source: dict, mapping: StandbyMapping) -> dict:
    deployment = copy.deepcopy(source)
    deployment.pop("status", None)
    deployment["metadata"] = _clean_metadata(deployment["metadata"])
    deployment["metadata"].update({"name": mapping.target, "namespace": NAMESPACE})
    labels = {
        "app.kubernetes.io/name": mapping.target,
        "auth": "external",
        "jw-market/standby": "true",
    }
    deployment["metadata"]["labels"] = labels
    deployment["spec"]["replicas"] = 1
    deployment["spec"]["selector"] = {"matchLabels": {"app.kubernetes.io/name": mapping.target}}
    template = deployment["spec"]["template"]
    template["metadata"] = {"labels": labels}
    pod_spec = template["spec"]
    pod_spec.pop("nodeName", None)
    container = pod_spec["containers"][0]
    mounts = container.setdefault("volumeMounts", [])
    _replace_named_entry(
        mounts,
        {
            "name": mapping.relay_configmap,
            "mountPath": "/app/src/main.py",
            "subPath": "main.py",
            "readOnly": True,
        },
    )
    volumes = pod_spec.setdefault("volumes", [])
    _replace_named_entry(
        volumes,
        {
            "name": mapping.relay_configmap,
            "configMap": {
                "name": mapping.relay_configmap,
                "defaultMode": 420,
                "items": [{"key": "main.py", "path": "main.py"}],
            },
        },
    )
    return deployment


def render_service(mapping: StandbyMapping) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{mapping.target}-svc",
            "namespace": NAMESPACE,
            "labels": {"jw-market/standby": "true"},
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"app.kubernetes.io/name": mapping.target},
            "ports": [{"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}],
        },
    }


def render_configmap(name: str, source: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"jw-market/standby": "true"},
        },
        "data": {"main.py": source},
    }


def render_stack(sources: Mapping[str, dict], relay_sources: Mapping[str, str]) -> list[dict]:
    objects = [render_configmap(name, relay_sources[name]) for name in RELAY_CONFIGMAPS]
    for mapping in STANDBY_MAPPINGS:
        objects.append(render_deployment(sources[mapping.source], mapping))
        objects.append(render_service(mapping))
    return objects


def deployment_contract(deployment: dict) -> dict:
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    return {
        "name": deployment["metadata"]["name"],
        "replicas": deployment["spec"].get("replicas"),
        "ownerReferences": deployment["metadata"].get("ownerReferences", []),
        "selector": deployment["spec"]["selector"],
        "labels": deployment["spec"]["template"]["metadata"].get("labels", {}),
        "nodeName": pod_spec.get("nodeName"),
        "image": container["image"],
        "command": container.get("command"),
        "args": container.get("args"),
        "env": container.get("env", []),
        "envFrom": container.get("envFrom", []),
        "ports": container.get("ports", []),
        "resources": container.get("resources", {}),
        "livenessProbe": container.get("livenessProbe"),
        "readinessProbe": container.get("readinessProbe"),
        "volumeMounts": container.get("volumeMounts", []),
        "volumes": pod_spec.get("volumes", []),
    }


def service_contract(service: dict) -> dict:
    return {
        "name": service["metadata"]["name"],
        "selector": service["spec"].get("selector", {}),
        "ports": service["spec"].get("ports", []),
        "type": service["spec"].get("type"),
    }


def configmap_contract(configmap: dict) -> dict:
    source = configmap["data"]["main.py"]
    return {
        "name": configmap["metadata"]["name"],
        "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
    }


def scan_tracked_files(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{path.relative_to(root)}:{name}")
    return findings


def chat_env_command() -> str:
    assignments = " ".join(f"{name}={value}" for name, value in CHAT_ENV.items())
    return f"kubectl -n {NAMESPACE} set env deployment/{CHAT_DEPLOYMENT} {assignments}"


def _kubectl_json(resource: str, name: str) -> dict:
    raw = subprocess.check_output(
        ["kubectl", "-n", NAMESPACE, "get", resource, name, "-o", "json"],
        text=True,
    )
    return json.loads(raw)


def _load_runtime_sources() -> dict[str, dict]:
    return {
        mapping.source: _kubectl_json("deployment", mapping.source)
        for mapping in STANDBY_MAPPINGS
    }


def _load_relay_sources() -> dict[str, str]:
    relay_dir = Path(__file__).with_name("relay")
    return {name: (relay_dir / f"{name}.py").read_text() for name in RELAY_CONFIGMAPS}


def _documents(objects: Sequence[dict]) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in objects) + "\n"


def _apply(objects: Sequence[dict], *, server_dry_run: bool = False) -> None:
    command = ["kubectl", "apply"]
    if server_dry_run:
        command.append("--dry-run=server")
    command.extend(["-f", "-"])
    subprocess.run(
        command,
        input=_documents(objects),
        text=True,
        check=True,
    )


def _wire_chat() -> None:
    command = [
        "kubectl",
        "-n",
        NAMESPACE,
        "set",
        "env",
        f"deployment/{CHAT_DEPLOYMENT}",
        *(f"{name}={value}" for name, value in CHAT_ENV.items()),
    ]
    subprocess.run(command, check=True)


def _verify_live(objects: Sequence[dict]) -> int:
    failures: list[str] = []
    for expected in objects:
        name = expected["metadata"]["name"]
        kind = expected["kind"]
        if kind == "Deployment":
            actual = _kubectl_json("deployment", name)
            expected_contract = deployment_contract(expected)
            actual_contract = deployment_contract(actual)
            matches = expected_contract == actual_contract
        elif kind == "Service":
            actual = _kubectl_json("service", name)
            expected_contract = service_contract(expected)
            actual_contract = service_contract(actual)
            matches = expected_contract == actual_contract
        elif kind == "ConfigMap":
            actual = _kubectl_json("configmap", name)
            expected_contract = configmap_contract(expected)
            actual_contract = configmap_contract(actual)
            matches = expected_contract == actual_contract
        else:
            matches = False
        if not matches:
            failures.append(f"{kind}/{name}")
            if kind in {"Deployment", "Service", "ConfigMap"}:
                differing = sorted(
                    key
                    for key in expected_contract.keys() | actual_contract.keys()
                    if expected_contract.get(key) != actual_contract.get(key)
                )
                print(f"drift_fields={kind}/{name}:{','.join(differing)}")
    checked = len(objects)
    population = len(objects)
    exit_code = 0 if checked > 0 and checked == population and not failures else 1
    print("gate=mcp_standby_live_contract")
    print("classification=census")
    print(f"checked={checked}")
    print(f"population={population}")
    print("missing=fail")
    print("tolerance=exact")
    print(f"failures={len(failures)}")
    print(f"exit_code={exit_code}")
    print("environment=llmops-runtime")
    for failure in failures:
        print(f"drift={failure}")
    return exit_code


def main(arguments: Sequence[str]) -> int:
    mode = arguments[1] if len(arguments) == 2 else ""
    if mode == "wire-chat":
        _wire_chat()
        return 0
    if mode not in {"apply", "dry-run-server", "verify-live"}:
        print(
            "usage: render_standby.py {apply|dry-run-server|verify-live|wire-chat}",
            file=sys.stderr,
        )
        return 2
    findings = scan_tracked_files(Path(__file__).parent)
    if findings:
        print("refusing to render credential-shaped tracked content", file=sys.stderr)
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    objects = render_stack(_load_runtime_sources(), _load_relay_sources())
    if mode == "verify-live":
        return _verify_live(objects)
    _apply(objects, server_dry_run=mode == "dry-run-server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
