from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


def find_response_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return value
        return None
    if isinstance(value, list):
        for item in value:
            found = find_response_text(item)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    for key_path in (
        ("data", "text"),
        ("data", "answer"),
        ("data", "output"),
        ("data", "result"),
        ("data", "response"),
        ("text",),
        ("answer",),
        ("output",),
        ("result",),
        ("response",),
    ):
        current: Any = value
        for key in key_path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        found = find_response_text(current)
        if found:
            return found
    for item in value.values():
        found = find_response_text(item)
        if found:
            return found
    return None


class Agent3WorkflowClient:
    def __init__(
        self,
        *,
        workflow_id: int = 316,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_s: int = 420,
    ) -> None:
        self.workflow_id = workflow_id
        self.endpoint = endpoint or os.environ.get("WF316_DIRECT_RUN_URL") or f"http://workflow-{workflow_id}.llmops.svc.cluster.local:8080/run/v2"
        self.timeout_s = timeout_s
        self.headers = {"Content-Type": "application/json"}
        resolved_token = token or os.environ.get("GENOS_BEARER_TOKEN") or os.environ.get("GENOS_TOKEN")
        if resolved_token:
            self.headers["Authorization"] = f"Bearer {resolved_token}"

    def run(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        chat_id = f"agent3-wf316-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()
        body = json.dumps(
            {
                "question": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "chatId": chat_id,
                "sessionId": chat_id,
                "overrideConfig": {"sessionId": chat_id},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers=self.headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            status = response.status
            raw_bytes = response.read()
        raw = json.loads(raw_bytes.decode("utf-8"))
        text = find_response_text(raw)
        if not text:
            raise RuntimeError("wf316 response did not contain a JSON text payload")
        parsed = json.loads(text[text.find("{") : text.rfind("}") + 1])
        if not isinstance(parsed, dict):
            raise RuntimeError("wf316 JSON payload is not an object")
        meta = {
            "http_status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "endpoint": self.endpoint,
        }
        return parsed, meta
