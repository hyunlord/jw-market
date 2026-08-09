"""Temporal worker that runs global Agent2 then Agent3 as guarded K8s Jobs."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from pipeline.scripts.agent_refresh_weekly.contract import (
    STAGE_ORDER,
    TASK_QUEUE,
    WORKFLOW_TYPE,
    classify_job_status,
    find_active_conflicts,
    make_job_name,
    render_stage_job,
)


_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_NAMESPACE = os.environ.get("POD_NAMESPACE", "llmops")
_OUTPUT_CLAIM = os.environ.get("MARKET_OUTPUT_PVC", "llmops-market-output")
_AGENT_JOB_IMAGE = os.environ.get("AGENT_JOB_IMAGE", "")
_POLL_SECONDS = int(os.environ.get("AGENT_REFRESH_POLL_SECONDS", "30"))
_CLUSTER_GUARD_SECONDS = int(
    os.environ.get("AGENT_REFRESH_CLUSTER_GUARD_INTERVAL_SECONDS", "600")
)
_GALERA_PODS = tuple(
    item.strip()
    for item in os.environ.get(
        "GALERA_PODS",
        "galera-mariadb-galera-0,galera-mariadb-galera-1,galera-mariadb-galera-2",
    ).split(",")
    if item.strip()
)


class KubernetesApi:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self._token = (_SA_DIR / "token").read_text(encoding="utf-8").strip()
        self._ssl = ssl.create_default_context(cafile=str(_SA_DIR / "ca.crt"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"https://kubernetes.default.svc{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(request, context=self._ssl, timeout=timeout) as response:
            payload = response.read()
        return json.loads(payload) if payload else {}

    def list_jobs(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        )
        return list(payload.get("items") or [])

    def get_job(self, name: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(name, safe="")
        return self._request(
            "GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{quoted}"
        )

    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", body=body
        )

    def delete_job(self, job: dict[str, Any]) -> dict[str, Any]:
        metadata = job.get("metadata") or {}
        name = str(metadata.get("name") or "")
        uid = str(metadata.get("uid") or "")
        resource_version = str(metadata.get("resourceVersion") or "")
        if not name or not uid or not resource_version:
            raise RuntimeError("owned Job deletion requires name, uid, and resourceVersion")
        quoted = urllib.parse.quote(name, safe="")
        return self._request(
            "DELETE",
            f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{quoted}",
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": uid, "resourceVersion": resource_version},
            },
        )


def _kubectl(*args: str, timeout: int = 30) -> str:
    completed = subprocess.run(
        ["kubectl", "-n", _NAMESPACE, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def _galera_probe(pod: str) -> dict[str, Any]:
    disk_raw = _kubectl(
        "exec",
        pod,
        "-c",
        "mariadb-galera",
        "--",
        "sh",
        "-lc",
        "df -Pk /bitnami/mariadb | awk 'NR==2 {print $2, $4, $5}'",
    )
    disk_fields = disk_raw.split()
    if len(disk_fields) != 3:
        raise RuntimeError(f"unexpected disk probe for {pod}: {disk_raw!r}")
    total_kib, available_kib = int(disk_fields[0]), int(disk_fields[1])
    client = "/opt/bitnami/mariadb/bin/mariadb"
    sql = "; ".join(
        f'{client} -N -B -uroot --password="$MARIADB_ROOT_PASSWORD" '
        f'-e "SHOW GLOBAL STATUS LIKE \'{name}\'"'
        for name in (
            "wsrep_cluster_size",
            "wsrep_cluster_status",
            "wsrep_local_state_comment",
            "wsrep_ready",
        )
    )
    status_raw = _kubectl(
        "exec", pod, "-c", "mariadb-galera", "--", "sh", "-lc", sql
    )
    values: dict[str, str] = {}
    for line in status_raw.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2:
            values[fields[0]] = fields[1]
    return {
        "pod": pod,
        "total_kib": total_kib,
        "available_kib": available_kib,
        "available_ratio": available_kib / total_kib,
        "wsrep_cluster_size": values.get("wsrep_cluster_size"),
        "wsrep_cluster_status": values.get("wsrep_cluster_status"),
        "wsrep_local_state_comment": values.get("wsrep_local_state_comment"),
        "wsrep_ready": values.get("wsrep_ready"),
    }


def _assert_cluster_guard() -> list[dict[str, Any]]:
    probes = [_galera_probe(pod) for pod in _GALERA_PODS]
    failures = []
    for probe in probes:
        if probe["available_ratio"] < 0.20:
            failures.append(f"{probe['pod']}:disk_below_20pct")
        if probe["wsrep_cluster_size"] != "3":
            failures.append(f"{probe['pod']}:cluster_size={probe['wsrep_cluster_size']}")
        if probe["wsrep_cluster_status"] != "Primary":
            failures.append(f"{probe['pod']}:status={probe['wsrep_cluster_status']}")
        if probe["wsrep_local_state_comment"] != "Synced":
            failures.append(f"{probe['pod']}:state={probe['wsrep_local_state_comment']}")
        if probe["wsrep_ready"] != "ON":
            failures.append(f"{probe['pod']}:ready={probe['wsrep_ready']}")
    if failures:
        raise RuntimeError("cluster guard failed: " + ", ".join(failures))
    return probes


def _job_logs(name: str) -> str:
    try:
        return _kubectl(
            "logs",
            f"job/{name}",
            "--all-containers=true",
            "--tail=200",
            timeout=60,
        )
    except Exception as exc:
        return f"logs unavailable: {type(exc).__name__}: {exc}"


def _preflight_sync(workflow_id: str) -> dict[str, Any]:
    api = KubernetesApi(_NAMESPACE)
    conflicts = find_active_conflicts(api.list_jobs())
    if conflicts:
        raise RuntimeError(f"active ingest/agent Job guard: {','.join(conflicts)}")
    probes = _assert_cluster_guard()
    return {"workflow_id": workflow_id, "galera": probes, "active_conflicts": []}


async def _run_stage(stage: str, workflow_id: str) -> dict[str, Any]:
    started_monotonic = asyncio.get_running_loop().time()
    api = KubernetesApi(_NAMESPACE)
    name = make_job_name(workflow_id, stage)
    conflicts = find_active_conflicts(
        await asyncio.to_thread(api.list_jobs), owned_job=name
    )
    if conflicts:
        raise RuntimeError(f"active ingest/agent Job guard: {','.join(conflicts)}")
    body = render_stage_job(
        stage=stage,
        workflow_id=workflow_id,
        image=_AGENT_JOB_IMAGE,
        namespace=_NAMESPACE,
        output_claim=_OUTPUT_CLAIM,
    )
    try:
        job = await asyncio.to_thread(api.create_job, body)
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        job = await asyncio.to_thread(api.get_job, name)
        labels = (job.get("metadata") or {}).get("labels") or {}
        if labels.get("app.kubernetes.io/managed-by") != "jw-agent-refresh-temporal":
            raise RuntimeError(f"job name collision with non-owned object: {name}") from exc

    try:
        next_cluster_guard = asyncio.get_running_loop().time() + _CLUSTER_GUARD_SECONDS
        while True:
            status = classify_job_status(job)
            activity.heartbeat({"stage": stage, "job": name, "status": status})
            if status == "Complete":
                await asyncio.to_thread(_assert_cluster_guard)
                return {
                    "stage": stage,
                    "job": name,
                    "status": status,
                    "attempt": activity.info().attempt,
                    "elapsed_seconds": round(
                        asyncio.get_running_loop().time() - started_monotonic, 3
                    ),
                    "started_at": (job.get("status") or {}).get("startTime"),
                    "completed_at": (job.get("status") or {}).get("completionTime"),
                }
            if status == "Failed":
                logs = await asyncio.to_thread(_job_logs, name)
                raise RuntimeError(f"stage Job failed: {name}\n{logs}")

            conflicts = find_active_conflicts(
                await asyncio.to_thread(api.list_jobs), owned_job=name
            )
            if conflicts:
                latest = await asyncio.to_thread(api.get_job, name)
                await asyncio.to_thread(api.delete_job, latest)
                raise RuntimeError(
                    "concurrent ingest/agent Job appeared; deleted owned stage Job "
                    f"{name}: {','.join(conflicts)}"
                )
            if asyncio.get_running_loop().time() >= next_cluster_guard:
                try:
                    await asyncio.to_thread(_assert_cluster_guard)
                except Exception:
                    latest = await asyncio.to_thread(api.get_job, name)
                    await asyncio.to_thread(api.delete_job, latest)
                    raise
                next_cluster_guard = (
                    asyncio.get_running_loop().time() + _CLUSTER_GUARD_SECONDS
                )
            await asyncio.sleep(_POLL_SECONDS)
            job = await asyncio.to_thread(api.get_job, name)
    except asyncio.CancelledError:
        try:
            latest = await asyncio.to_thread(api.get_job, name)
            if classify_job_status(latest) in {"Pending", "Running"}:
                await asyncio.to_thread(api.delete_job, latest)
        except Exception as cleanup_exc:
            activity.logger.error(
                "owned Job cleanup failed job=%s error=%s", name, cleanup_exc
            )
        raise


@activity.defn(name="agent_refresh_weekly_preflight")
async def preflight_activity(payload: dict[str, str]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_preflight_sync, payload["workflow_id"])
    except Exception as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn(name="agent_refresh_weekly_run_stage")
async def run_stage_activity(payload: dict[str, str]) -> dict[str, Any]:
    try:
        return await _run_stage(payload["stage"], payload["workflow_id"])
    except urllib.error.URLError as exc:
        raise ApplicationError(str(exc), type="TransientKubernetesError") from exc
    except Exception as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@workflow.defn(name=WORKFLOW_TYPE)
class WeeklyAgentRefreshWorkflow:
    @workflow.run
    async def run(self) -> dict[str, Any]:
        workflow_id = workflow.info().workflow_id
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=2,
        )
        preflight = await workflow.execute_activity(
            preflight_activity,
            {"workflow_id": workflow_id},
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )
        stages = []
        for stage in STAGE_ORDER:
            stages.append(
                await workflow.execute_activity(
                    run_stage_activity,
                    {"stage": stage, "workflow_id": workflow_id},
                    start_to_close_timeout=(
                        timedelta(hours=7)
                        if stage == "agent2"
                        else timedelta(hours=3)
                    ),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=retry,
                )
            )
        return {"status": "complete", "preflight": preflight, "stages": stages}


async def _main() -> None:
    if not _AGENT_JOB_IMAGE:
        raise RuntimeError("AGENT_JOB_IMAGE is required")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
        workflows=[WeeklyAgentRefreshWorkflow],
        activities=[preflight_activity, run_stage_activity],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
