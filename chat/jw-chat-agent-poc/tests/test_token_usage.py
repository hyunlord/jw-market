from __future__ import annotations

import json
from typing import Any

from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner
from jw_chat_agent_poc.common.timing import public_payload
from jw_chat_agent_poc.common.token_usage import record_token_usage, usage_call_from_payload
from jw_chat_agent_poc.service.genos_client import GenosClient


class _StreamResponse:
    def __init__(self, lines: tuple[str, ...]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, *, decode_unicode: bool = False):
        yield from self._lines


class _JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def test_stream_chat_requests_usage_and_records_final_usage(monkeypatch) -> None:
    captured_body: dict[str, Any] = {}
    usage_payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": "gemini-3-flash-preview",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
    }

    def post(_url: str, **kwargs):
        captured_body.update(kwargs["json"])
        return _StreamResponse(
            (
                'data: {"choices":[{"delta":{"content":"안녕"},"index":0}],"model":"gemini-3-flash-preview"}',
                f"data: {json.dumps(usage_payload)}",
                "data: [DONE]",
            )
        )

    monkeypatch.setattr("jw_chat_agent_poc.service.genos_client.requests.post", post)
    client = GenosClient(base_url="https://example.test/api/gateway/rep/serving/190", token="token")

    text = "".join(client._stream_chat([{"role": "user", "content": "hi"}]))

    assert text == "안녕"
    assert captured_body["stream_options"] == {"include_usage": True}
    assert client.token_usage_calls == [
        {
            "model": "gemini-3-flash-preview",
            "serving_id": "190",
            "stream": True,
            "input_tokens": 7,
            "output_tokens": 11,
            "total_tokens": 18,
            "raw_usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
        }
    ]


def test_public_timing_payload_exposes_aggregate_token_usage() -> None:
    timing: dict[str, Any] = {"started_at_monotonic": 1.0, "total_elapsed_ms": 42.0, "stages": []}

    record_token_usage(
        timing,
        usage_call_from_payload(
            {
                "model": "gemini-3-flash-preview",
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            },
            base_url="https://example.test/api/gateway/rep/serving/190",
            stream=False,
        ),
    )

    payload = public_payload(timing)

    assert payload["token_usage"]["available"] is True
    assert payload["token_usage"]["total_input_tokens"] == 3
    assert payload["token_usage"]["total_output_tokens"] == 5
    assert payload["token_usage"]["total_tokens"] == 8
    assert payload["token_usage"]["calls"][0]["serving_id"] == "190"


def test_tool_planner_records_nonstream_usage(monkeypatch) -> None:
    response_payload = {
        "model": "gemini-3-flash-preview",
        "choices": [{"message": {"content": "도구 결과로 답변하세요."}}],
        "usage": {"prompt_tokens": 13, "completion_tokens": 2, "total_tokens": 15},
    }

    def post(_url: str, **_kwargs):
        return _JsonResponse(response_payload)

    monkeypatch.setattr("requests.post", post)
    planner = GenosToolPlanner(base_url="https://example.test/api/gateway/rep/serving/190", token="token")

    decision = planner.decide("리바로 매출", (), (), (), ())

    assert decision.final_answer == "도구 결과로 답변하세요."
    assert planner.last_token_usage is not None
    assert planner.last_token_usage["input_tokens"] == 13
    assert planner.last_token_usage["output_tokens"] == 2
