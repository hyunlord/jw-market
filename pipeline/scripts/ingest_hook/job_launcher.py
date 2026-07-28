"""Render and submit the incremental ingest Job (batch/v1).

The trigger service is the only writer of Jobs and holds a Role limited to
jobs create/get/list/delete in its own namespace. The Job body mirrors
deploy/k8s/ingest-hook/ingest-job-template.yaml; the manifest path and
category labels are the only per-submission fields.

Submission goes through an injectable ``transport`` callable so tests and
isolation rehearsals never talk to a real API server.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline.scripts.ingest_hook import config

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# Env the Job inherits from the trigger pod, by reference where secret-backed.
_PASSTHROUGH_VALUES = (
    "APP_VERSION",
    "INGEST_JOB_IMAGE",
    "MARIADB_HOST", "MARIADB_PORT", "MARIADB_DATABASE", "AGENT3_DB_NAME",
    "MINIO_ENDPOINT", "MINIO_REGION",
    "AGENT3_WORKFLOW_REV", "AGENT3_EXPECTED_WORKFLOW_REV",
    "INGEST_REHEARSAL_ROOT",  # C-phase isolation: staging stays pod-local
    "INGEST_LOAD_STAGING_ROOT",  # J5 real-loader staging output (mart refresh skipped)
    "INGEST_LOAD_STAGING_DB",    # isolated jw_ingest_* target for table adapters
    "INGEST_LOAD_SHADOW_ROOT",   # full gates with isolated corpus + mart only
    "INGEST_SHADOW_LEDGER_SQLITE",
    "INGEST_SHADOW_TARGET_DB",
    "INGEST_SHADOW_BUILD_PREFIX",
    "INGEST_SHADOW_CATALOG_ROOT",
    "INGEST_SHADOW_SEED_ROOT",
    "INGEST_SHADOW_FAILURE_AT",
    "INGEST_SHADOW_CRASH_AT",
    "INGEST_LOAD_TARGET_ROOT",   # J5 production load output root (D-3; refresh runs)
    "INGEST_MART_PROMOTION_APPROVED",
    "INGEST_MART_SOURCE_DB",
    "INGEST_MART_TARGET_DB",
    "INGEST_MART_BUILD_PREFIX",
    "JW_MARKET_CATALOG_ROOT",
    "INGEST_CATALOG_IQVIA_NSA_DIR",
    "INGEST_INPUT_BACKEND",
    "INGEST_INPUT_ROOT",
    "INGEST_COMPLETION_WEBHOOK_URL",
    "INGEST_COMPLETION_WEBHOOK_ATTEMPTS",
)
_MART_SECRET = "jw-mart-d2-writer"
_PORTAL_SECRET = "jw-data-portal-secrets"      # bucket name (site-owned)
_MINIO_READ_SECRET = "jw-ingest-hook-minio"     # hook-owned read-only credentials
_LOCAL_INPUT_VOLUME = "ingest-input"
_LOCAL_INPUT_PVC = "llmops-nfs-root"
_LOCAL_INPUT_SUB_PATH = "autoIngestion"
_MARKET_OUTPUT_VOLUME = "market-output"


def _job_env() -> list[dict]:
    env: list[dict] = [
        {"name": name, "value": os.environ[name]}
        for name in _PASSTHROUGH_VALUES
        if os.environ.get(name)
    ]

    def secret_ref(name, secret, key):
        env.append({"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})

    secret_ref("MARIADB_USER", _MART_SECRET, "username")
    secret_ref("MARIADB_PASSWORD", _MART_SECRET, "password")
    local_input = os.environ.get(config.ENV_INPUT_BACKEND, "").strip().lower() == "local"
    if not local_input and os.environ.get("INGEST_S3_BUCKET"):
        secret_ref("INGEST_S3_BUCKET", _PORTAL_SECRET, "MINIO_MARKET_BUCKET")
        # The portal account is write/list-only by policy; the hook reads with
        # its own read-only MinIO user (GetObject+ListBucket on the bucket).
        secret_ref("MINIO_ACCESS_KEY", _MINIO_READ_SECRET, "MINIO_ACCESS_KEY")
        secret_ref("MINIO_SECRET_KEY", _MINIO_READ_SECRET, "MINIO_SECRET_KEY")
    return env

Transport = Callable[[str, dict], dict]
InspectTransport = Callable[[str, str], dict]
DeleteTransport = Callable[[str, dict], dict]


@dataclass(frozen=True)
class JobObservation:
    status: str  # Pending | Running | Complete | Failed | Absent
    reason: str | None
    evidence: dict


class JobSubmissionConflict(RuntimeError):
    def __init__(
        self,
        *,
        job_name: str,
        existing_status: str,
        created_at: str | None,
        inspection_error: str | None = None,
    ):
        self.job_name = job_name
        self.existing_status = existing_status
        self.created_at = created_at
        self.inspection_error = inspection_error
        inspection_detail = (
            f" inspection_error={inspection_error}" if inspection_error else ""
        )
        super().__init__(
            f"job_name={job_name} existing_status={existing_status} "
            f"created_at={created_at or 'unknown'}{inspection_detail}"
        )


def job_name(category: str, manifest_sha: str, run_id: str | None = None) -> str:
    base = f"jw-ingest-{category.replace('_', '-')}-{manifest_sha[:8]}"
    if run_id is None:
        return base
    suffix = "".join(char.lower() for char in run_id if char.isalnum() or char == "-")
    if not suffix:
        raise ValueError("run_id must contain a Kubernetes name character")
    return f"{base[:62 - len(suffix)]}-{suffix}".rstrip("-")


def render_job(
    *,
    category: str,
    manifest_sha: str,
    manifest_path: str,
    namespace: str | None = None,
    run_id: str | None = None,
) -> dict:
    name = job_name(category, manifest_sha, run_id)
    local_root = (
        config.input_root()
        if os.environ.get(config.ENV_INPUT_BACKEND, "").strip().lower() == "local"
        else None
    )
    volume_mounts = (
        [
            {
                "name": _LOCAL_INPUT_VOLUME,
                "mountPath": str(local_root),
                "subPath": _LOCAL_INPUT_SUB_PATH,
                "readOnly": True,
            }
        ]
        if local_root is not None
        else []
    )
    volumes = (
        [{"name": _LOCAL_INPUT_VOLUME, "persistentVolumeClaim": {"claimName": _LOCAL_INPUT_PVC}}]
        if local_root is not None
        else []
    )
    output_root = config.load_target_mount_root()
    resources = (
        {
            "requests": {"cpu": "2", "memory": "8Gi"},
            "limits": {"cpu": "4", "memory": "16Gi"},
        }
        if config.load_mode(required=False) == "shadow"
        else {
            # The full 61-month mart calculation reached the 8 GiB cgroup
            # ceiling. Keep request equal to limit so scheduling reserves the
            # approved 12 GiB headroom before the Job starts.
            "requests": {"cpu": "2", "memory": "12Gi"},
            "limits": {"cpu": "2", "memory": "12Gi"},
        }
    )
    if not any(item["name"] == _MARKET_OUTPUT_VOLUME for item in volume_mounts):
        volume_mounts.append(
            {
                "name": _MARKET_OUTPUT_VOLUME,
                "mountPath": str(config.MARKET_OUTPUT_ROOT),
                "readOnly": False,
            }
        )
        volumes.append(
            {
                "name": _MARKET_OUTPUT_VOLUME,
                "persistentVolumeClaim": {"claimName": config.MARKET_OUTPUT_PVC},
            }
        )
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
                    # api-01 can mount both NFS volumes while db-02 times out
                    # before the ingest container starts. Keep Jobs pending
                    # instead of scheduling them onto a node that cannot run.
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": "cloud.google.com/gke-nodepool",
                                                "operator": "In",
                                                "values": [
                                                    "knp-jw-agn-dev-genos-api-01"
                                                ],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "ingest",
                            "image": config.job_image(),
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "python",
                                "-m",
                                "pipeline.scripts.ingest_hook.stage_log_runner",
                                "--manifest",
                                manifest_path,
                                "--run-id",
                                run_id or name,
                                "--job-name",
                                name,
                            ],
                            "env": [
                                *_job_env(),
                                {"name": config.ENV_LOG_ROOT, "value": config.DEFAULT_LOG_ROOT},
                            ],
                            **({"volumeMounts": volume_mounts} if volume_mounts else {}),
                            "resources": resources,
                        }
                    ],
                    **({"volumes": volumes} if volumes else {}),
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


def _in_cluster_inspect(namespace: str, name: str) -> dict:
    token = (_SA_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(_SA_DIR / "ca.crt"))
    request = urllib.request.Request(
        f"https://kubernetes.default.svc/apis/batch/v1/namespaces/{namespace}/jobs/{name}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _in_cluster_delete(url_path: str, body: dict) -> dict:
    token = (_SA_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(_SA_DIR / "ca.crt"))
    request = urllib.request.Request(
        f"https://kubernetes.default.svc{url_path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="DELETE",
    )
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _job_status(payload: dict) -> str:
    status = payload.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("status") == "True" and condition.get("type") in {"Complete", "Failed"}:
            return str(condition["type"])
    if status.get("active"):
        return "Running"
    if status.get("failed"):
        return "Failed"
    if status.get("succeeded"):
        return "Complete"
    return "Pending"


def inspect_job(
    name: str,
    *,
    namespace: str | None = None,
    transport: InspectTransport | None = None,
) -> JobObservation:
    """Read one Job and retain a bounded condition summary for durable evidence."""
    inspect = transport or _in_cluster_inspect
    resolved_namespace = namespace or config.job_namespace()
    try:
        payload = inspect(resolved_namespace, name)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return JobObservation(
            status="Absent",
            reason="NotFound",
            evidence={"job_name": name, "job_status": "Absent", "conditions": []},
        )

    status = _job_status(payload)
    raw_status = payload.get("status") or {}
    conditions = []
    for item in raw_status.get("conditions") or []:
        conditions.append(
            {
                "type": str(item.get("type") or ""),
                "status": str(item.get("status") or ""),
                "reason": str(item.get("reason") or ""),
                "message": str(item.get("message") or "")[:1000],
                "last_transition_time": item.get("lastTransitionTime"),
            }
        )
    terminal = next(
        (
            item
            for item in conditions
            if item["status"] == "True" and item["type"] in {"Complete", "Failed"}
        ),
        None,
    )
    metadata = payload.get("metadata") or {}
    return JobObservation(
        status=status,
        reason=(terminal or {}).get("reason") or None,
        evidence={
            "job_name": name,
            "job_status": status,
            "job_uid": metadata.get("uid"),
            "resource_version": metadata.get("resourceVersion"),
            "created_at": metadata.get("creationTimestamp"),
            "active": int(raw_status.get("active") or 0),
            "failed": int(raw_status.get("failed") or 0),
            "succeeded": int(raw_status.get("succeeded") or 0),
            "conditions": conditions,
        },
    )


def delete_job(
    name: str,
    *,
    observation: JobObservation,
    namespace: str | None = None,
    transport: DeleteTransport | None = None,
) -> None:
    """Delete one exact observed Job with Kubernetes identity preconditions."""
    if observation.evidence.get("job_name") != name:
        raise ValueError("observed Job identity does not match the deletion target")
    uid = observation.evidence.get("job_uid")
    resource_version = observation.evidence.get("resource_version")
    if not uid or not resource_version:
        raise ValueError("Kubernetes Job UID/resourceVersion is required for force stop")
    resolved_namespace = namespace or config.job_namespace()
    path = f"/apis/batch/v1/namespaces/{resolved_namespace}/jobs/{name}"
    body = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "gracePeriodSeconds": 0,
        "propagationPolicy": "Foreground",
        "preconditions": {
            "uid": uid,
            "resourceVersion": resource_version,
        },
    }
    send = transport or _in_cluster_delete
    try:
        send(path, body)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def submit_job(
    *,
    category: str,
    manifest_sha: str,
    manifest_path: str,
    transport: Transport | None = None,
    namespace: str | None = None,
    run_id: str | None = None,
    inspect_transport: InspectTransport | None = None,
) -> str:
    """Create the Job and return its name."""
    body = render_job(
        category=category,
        manifest_sha=manifest_sha,
        manifest_path=manifest_path,
        namespace=namespace,
        run_id=run_id,
    )
    send = transport or _in_cluster_transport
    try:
        send(f"/apis/batch/v1/namespaces/{body['metadata']['namespace']}/jobs", body)
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        inspect = inspect_transport or _in_cluster_inspect
        try:
            existing = inspect(
                body["metadata"]["namespace"], body["metadata"]["name"]
            )
        except Exception as inspect_exc:
            error_name = type(inspect_exc).__name__
            error_code = getattr(inspect_exc, "code", None)
            inspection_error = (
                f"{error_name}:{error_code}" if error_code is not None else error_name
            )
            raise JobSubmissionConflict(
                job_name=body["metadata"]["name"],
                existing_status="Unknown",
                created_at=None,
                inspection_error=inspection_error,
            ) from exc
        metadata = existing.get("metadata") or {}
        existing_status = _job_status(existing)
        if existing_status in {"Pending", "Running"}:
            return body["metadata"]["name"]
        raise JobSubmissionConflict(
            job_name=body["metadata"]["name"],
            existing_status=existing_status,
            created_at=metadata.get("creationTimestamp"),
        ) from exc
    return body["metadata"]["name"]
