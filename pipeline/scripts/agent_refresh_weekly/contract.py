"""Pure contracts shared by the weekly Temporal worker and its tests."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final, Iterable


STAGE_ORDER: Final = ("agent2", "agent3")
SCHEDULE_ID: Final = "jw-agent2-agent3-weekly-v1"
WORKFLOW_TYPE: Final = "jw_agent2_agent3_weekly_v1"
TASK_QUEUE: Final = "jw-agent-refresh-weekly-v1"

_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_CONFLICT_PREFIXES: Final = (
    "jw-agent-refresh-",
    "jw-agent3-refresh-daily-",
    "jw-ingest-",
)
_CONFLICT_APPS: Final = {"jw-complete-reingest"}


def _run_token(workflow_id: str) -> str:
    return hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:12]


def make_job_name(workflow_id: str, stage: str) -> str:
    if stage not in STAGE_ORDER:
        raise ValueError(f"unsupported stage: {stage}")
    return f"jw-agent-refresh-weekly-{stage}-{_run_token(workflow_id)}"


def classify_job_status(job: dict[str, Any]) -> str:
    status = job.get("status") or {}
    terminal = {
        str(condition.get("type")): str(condition.get("status"))
        for condition in status.get("conditions") or []
    }
    if terminal.get("Complete") == "True":
        return "Complete"
    if terminal.get("Failed") == "True":
        return "Failed"
    if int(status.get("active") or 0) > 0 or int(status.get("terminating") or 0) > 0:
        return "Running"
    if int(status.get("succeeded") or 0) > 0:
        return "Complete"
    return "Pending"


def find_active_conflicts(
    jobs: Iterable[dict[str, Any]], *, owned_job: str | None = None
) -> tuple[str, ...]:
    conflicts: set[str] = set()
    for job in jobs:
        metadata = job.get("metadata") or {}
        name = str(metadata.get("name") or "")
        if not name or name == owned_job:
            continue
        labels = metadata.get("labels") or {}
        is_ingest_or_agent = (
            name.startswith(_CONFLICT_PREFIXES)
            or str(labels.get("app") or "") in _CONFLICT_APPS
            or any(str(key).startswith("jw.ingest/") for key in labels)
        )
        if not is_ingest_or_agent:
            continue
        if classify_job_status(job) in {"Pending", "Running"}:
            conflicts.add(name)
    return tuple(sorted(conflicts))


def _secret_env(name: str, key: str) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {"name": "jw-mart-d2-writer", "key": key}
        },
    }


def _common_env(run_token: str) -> list[dict[str, Any]]:
    return [
        {"name": "WEEKLY_RUN_ID", "value": run_token},
        {"name": "DB_HOST", "value": "llmops-mariadb-service.llmops.svc.cluster.local"},
        {"name": "DB_PORT", "value": "3306"},
        {"name": "DB_NAME", "value": "jw_mart_d2_stage_20260630_r2"},
        _secret_env("DB_USER", "username"),
        _secret_env("DB_ROOT_PASSWORD", "password"),
        _secret_env("DB_PASSWORD", "password"),
        {"name": "AGENT3_DB_HOST", "value": "llmops-mariadb-service.llmops.svc.cluster.local"},
        {"name": "AGENT3_DB_PORT", "value": "3306"},
        {"name": "AGENT3_DB_NAME", "value": "jw_mart_d2_stage_20260630_r2"},
        _secret_env("AGENT3_DB_USER", "username"),
        _secret_env("AGENT3_DB_PASSWORD", "password"),
        {"name": "WF316_DIRECT_RUN_URL", "value": "http://workflow-316.llmops.svc.cluster.local:8080/run/v2"},
        {"name": "AGENT3_WORKFLOW_REV", "value": "5692"},
        {"name": "AGENT3_EXPECTED_WORKFLOW_REV", "value": "5692"},
        {"name": "NPY_DISABLE_CPU_FEATURES", "value": "X86_V3,X86_V4"},
        {"name": "OPENBLAS_CORETYPE", "value": "Nehalem"},
        {"name": "OMP_NUM_THREADS", "value": "1"},
        {"name": "OPENBLAS_NUM_THREADS", "value": "1"},
        {"name": "MKL_NUM_THREADS", "value": "1"},
        {"name": "NUMEXPR_NUM_THREADS", "value": "1"},
    ]


def _stage_script(stage: str) -> str:
    root = "/market-output/agent-refresh-weekly/${WEEKLY_RUN_ID}"
    if stage == "agent2":
        return f"""set -euo pipefail
cd /app
mkdir -p \"{root}/short\" \"{root}/long\"
python -m pipeline.scripts.ai_analysis.agent2_regen_orchestrator \\
  --brand-source general-density \\
  --bundle-kind general \\
  --dry-run \\
  --analysis-variant short \\
  --work-dir \"{root}/short\"
python -m pipeline.scripts.ai_analysis.agent2_regen_orchestrator \\
  --brand-source general-density \\
  --bundle-kind general \\
  --dry-run \\
  --analysis-variant long \\
  --work-dir \"{root}/long\"
"""
    if stage == "agent3":
        return f"""set -euo pipefail
cd /app
mkdir -p \"{root}\"
python -m pipeline.scripts.agent3.run_source \\
  --brand-source general_all \\
  --mode full \\
  --source all \\
  --expected-workflow-rev 5692 \\
  --output \"{root}/agent3.json\"
cat \"{root}/agent3.json\"
"""
    raise ValueError(f"unsupported stage: {stage}")


def render_stage_job(
    *,
    stage: str,
    workflow_id: str,
    image: str,
    namespace: str,
    output_claim: str,
) -> dict[str, Any]:
    if not _IMAGE_DIGEST_RE.fullmatch(image):
        raise ValueError("agent Job image must use an immutable digest")
    name = make_job_name(workflow_id, stage)
    run_token = _run_token(workflow_id)
    deadline = 23_400 if stage == "agent2" else 10_800
    resources = (
        {
            "requests": {"cpu": "2", "memory": "12Gi"},
            "limits": {"cpu": "2", "memory": "12Gi"},
        }
        if stage == "agent2"
        else {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        }
    )
    labels = {
        "app": "jw-agent-refresh",
        "component": "weekly-agent-refresh",
        "jw-agent-refresh/stage": stage,
        "jw-agent-refresh/run": run_token,
        "app.kubernetes.io/managed-by": "jw-agent-refresh-temporal",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": 604_800,
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {
                        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
                        "sidecar.istio.io/inject": "false",
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 3000,
                        "runAsGroup": 3000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": "cloud.google.com/gke-nodepool",
                                                "operator": "In",
                                                "values": ["knp-jw-agn-dev-genos-api-01"],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    "containers": [
                        {
                            "name": stage,
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/bash", "-lc"],
                            "args": [_stage_script(stage)],
                            "env": _common_env(run_token),
                            "resources": resources,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {
                                    "name": "market-output",
                                    "mountPath": "/market-output",
                                    "readOnly": False,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "market-output",
                            "persistentVolumeClaim": {"claimName": output_claim},
                        }
                    ],
                },
            },
        },
    }
