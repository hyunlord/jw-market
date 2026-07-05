from __future__ import annotations

import io
import json
import urllib.error

import pytest

from pipeline.scripts.agent3 import workflow_client
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient, WorkflowHttpError, WorkflowRetryExhaustedError


class FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"data": {"text": json.dumps(self.payload)}}).encode("utf-8")


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://wf316", status, "err", {}, io.BytesIO(b"temporary failure"))


def test_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    sleeps: list[int] = []
    responses: list[object] = [_http_error(500), _http_error(502), FakeResponse({"strength_items": []})]

    def fake_urlopen(request: object, timeout: int) -> object:
        calls.append(timeout)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(workflow_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(workflow_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    summary, meta = Agent3WorkflowClient(endpoint="http://wf316", timeout_s=7).run({"brand": "x"})

    assert summary == {"strength_items": []}
    assert meta["http_attempts"] == 3
    assert calls == [7, 7, 7]
    assert sleeps == [5, 15]


def test_5xx_retry_exhaustion_raises_retry_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []

    def fake_urlopen(request: object, timeout: int) -> object:
        raise _http_error(500)

    monkeypatch.setattr(workflow_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(workflow_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(WorkflowRetryExhaustedError) as exc_info:
        Agent3WorkflowClient(endpoint="http://wf316").run({"brand": "x"})

    assert exc_info.value.attempts == 4
    assert "temporary failure" in exc_info.value.last_error
    assert sleeps == [5, 15, 45]


def test_4xx_fails_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []

    def fake_urlopen(request: object, timeout: int) -> object:
        raise _http_error(400)

    monkeypatch.setattr(workflow_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(workflow_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(WorkflowHttpError) as exc_info:
        Agent3WorkflowClient(endpoint="http://wf316").run({"brand": "x"})

    assert exc_info.value.status == 400
    assert exc_info.value.attempts == 1
    assert sleeps == []

