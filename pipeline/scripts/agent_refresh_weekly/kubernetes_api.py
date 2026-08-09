"""Minimal in-cluster Kubernetes API client for weekly refresh Jobs."""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubernetesApi:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self._token = (_SERVICE_ACCOUNT_DIR / "token").read_text(
            encoding="utf-8"
        ).strip()
        self._ssl = ssl.create_default_context(
            cafile=str(_SERVICE_ACCOUNT_DIR / "ca.crt")
        )

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
        with urllib.request.urlopen(
            request, context=self._ssl, timeout=timeout
        ) as response:
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
            raise RuntimeError(
                "owned Job deletion requires name, uid, and resourceVersion"
            )
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
