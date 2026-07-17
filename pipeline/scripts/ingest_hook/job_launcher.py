"""Render and submit the incremental ingest Job (batch/v1).

The trigger service is the only writer of Jobs and holds a Role limited to
jobs create/get/list in its own namespace. The Job body mirrors
deploy/k8s/ingest-hook/ingest-job-template.yaml; the manifest path and
category labels are the only per-submission fields.

Submission goes through an injectable ``transport`` callable so tests and
isolation rehearsals never talk to a real API server.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from collections.abc import Callable
from pathlib import Path

from pipeline.scripts.ingest_hook import config

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# Env the Job inherits from the trigger pod, by reference where secret-backed.
_PASSTHROUGH_VALUES = (
    "MARIADB_HOST", "MARIADB_PORT", "MARIADB_DATABASE", "AGENT3_DB_NAME",
    "MINIO_ENDPOINT", "MINIO_REGION",
    "AGENT3_WORKFLOW_REV", "AGENT3_EXPECTED_WORKFLOW_REV",
    "INGEST_REHEARSAL_ROOT",  # C-phase isolation: staging stays pod-local
)
_MART_SECRET = "jw-mart-d2-writer"
_PORTAL_SECRET = "jw-data-portal-secrets"      # bucket name (site-owned)
_MINIO_READ_SECRET = "jw-ingest-hook-minio"     # hook-owned read-only credentials


def _job_env() -> list[dict]:
    import os

    env: list[dict] = [
        {"name": name, "value": os.environ[name]}
        for name in _PASSTHROUGH_VALUES
        if os.environ.get(name)
    ]

    def secret_ref(name, secret, key):
        env.append({"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})

    secret_ref("MARIADB_USER", _MART_SECRET, "username")
    secret_ref("MARIADB_PASSWORD", _MART_SECRET, "password")
    if os.environ.get("INGEST_S3_BUCKET"):
        secret_ref("INGEST_S3_BUCKET", _PORTAL_SECRET, "MINIO_MARKET_BUCKET")
        # The portal account is write/list-only by policy; the hook reads with
        # its own read-only MinIO user (GetObject+ListBucket on the bucket).
        secret_ref("MINIO_ACCESS_KEY", _MINIO_READ_SECRET, "MINIO_ACCESS_KEY")
        secret_ref("MINIO_SECRET_KEY", _MINIO_READ_SECRET, "MINIO_SECRET_KEY")
    return env

Transport = Callable[[str, dict], dict]


def job_name(category: str, manifest_sha: str) -> str:
    return f"jw-ingest-{category}-{manifest_sha[:8]}"


def render_job(*, category: str, manifest_sha: str, manifest_path: str, namespace: str | None = None) -> dict:
    name = job_name(category, manifest_sha)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace or config.job_namespace(),
            "labels": {
                "app": "jw-ingest",
                "jw-ingest/category": category,
                "jw-ingest/manifest-sha8": manifest_sha[:8],
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 21600,
            "ttlSecondsAfterFinished": 259200,
            "template": {
                "metadata": {"labels": {"app": "jw-ingest"}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ingest",
                            "image": config.job_image(),
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "python",
                                "-m",
                                "pipeline.scripts.ingest_hook.job_runner",
                                "--manifest",
                                manifest_path,
                            ],
                            "env": _job_env(),
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "1Gi"},
                                "limits": {"cpu": "2", "memory": "4Gi"},
                            },
                        }
                    ],
                },
            },
        },
    }


def _in_cluster_transport(url_path: str, body: dict) -> dict:
    token = (_SA_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(_SA_DIR / "ca.crt"))
    request = urllib.request.Request(
        f"https://kubernetes.default.svc{url_path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def submit_job(
    *,
    category: str,
    manifest_sha: str,
    manifest_path: str,
    transport: Transport | None = None,
    namespace: str | None = None,
) -> str:
    """Create the Job and return its name."""
    body = render_job(
        category=category, manifest_sha=manifest_sha, manifest_path=manifest_path, namespace=namespace
    )
    send = transport or _in_cluster_transport
    send(f"/apis/batch/v1/namespaces/{body['metadata']['namespace']}/jobs", body)
    return body["metadata"]["name"]
