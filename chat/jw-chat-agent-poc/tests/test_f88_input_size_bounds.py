from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import create_app
from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult
from jw_chat_agent_poc.service.models import ChatRequest


QUESTION_MAX_CHARS = 8_000
DIRECT_FILE_CONTEXT_MAX_CHARS = 8_000
COMBINED_FILE_CONTEXT_MAX_CHARS = 32_000


class _EchoAgent:
    def __init__(self, *, external_mode: str = "live") -> None:
        self.external_mode = external_mode

    def answer(self, question: str, _documents=None) -> dict:
        return {"answer": f"ok:{question}", "sources": [], "tool_calls": []}


def _echo_factory(*, external_mode: str = "live") -> _EchoAgent:
    return _EchoAgent(external_mode=external_mode)


def test_chat_request_rejects_oversized_question() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(question="q" * (QUESTION_MAX_CHARS + 1))


def test_chat_request_rejects_oversized_direct_file_context() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(
            question="파일 요약",
            file_context="f" * (DIRECT_FILE_CONTEXT_MAX_CHARS + 1),
        )


def test_chat_request_accepts_established_bridge_context_limit() -> None:
    request = ChatRequest(
        question="파일 요약",
        file_context="f" * DIRECT_FILE_CONTEXT_MAX_CHARS,
    )

    assert len(request.file_context or "") == DIRECT_FILE_CONTEXT_MAX_CHARS


@pytest.mark.parametrize("path", ("/chat", "/chat/answer"))
@pytest.mark.parametrize(
    "payload",
    (
        {"question": "q" * (QUESTION_MAX_CHARS + 1)},
        {
            "question": "파일 요약",
            "file_context": "f" * (DIRECT_FILE_CONTEXT_MAX_CHARS + 1),
        },
    ),
)
def test_rest_routes_reject_oversized_direct_input_before_dispatch(
    monkeypatch,
    path: str,
    payload: dict[str, str],
) -> None:
    dispatched = False

    def answer(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("oversized input reached dispatch")

    monkeypatch.setattr(service_app, "_answer_question", answer)
    client = TestClient(create_app(agent_factory=_echo_factory))

    response = client.post(path, json=payload)

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "input_too_large"
    assert dispatched is False


def test_chat_stream_rejects_oversized_question_before_dispatch(monkeypatch) -> None:
    dispatched = False

    def answer(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("oversized question reached dispatch")

    monkeypatch.setattr(service_app, "_answer_question", answer)
    client = TestClient(create_app(agent_factory=_echo_factory))

    response = client.get(
        "/chat/stream",
        params={"question": "q" * (QUESTION_MAX_CHARS + 1)},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "input_too_large"
    assert "q" * 100 not in response.text
    assert dispatched is False


def test_delegated_context_rejects_oversized_combined_input(monkeypatch) -> None:
    uploaded = UploadedFileSearchResult(
        file_context="b" * 24_001,
        file_sources=(),
        errors=(),
    )
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda *_args: uploaded)

    with pytest.raises(service_app.InputSizeLimitError):
        service_app._delegated_file_context(
            "파일 요약",
            "conversation-1",
            "d" * DIRECT_FILE_CONTEXT_MAX_CHARS,
        )


def test_delegated_context_accepts_bridge_and_direct_context_within_total(monkeypatch) -> None:
    uploaded = UploadedFileSearchResult(
        file_context="b" * 24_000,
        file_sources=(),
        errors=(),
    )
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda *_args: uploaded)

    delegated = service_app._delegated_file_context(
        "파일 요약",
        "conversation-1",
        "d" * (DIRECT_FILE_CONTEXT_MAX_CHARS - 2),
    )

    assert len(delegated[0] or "") == COMBINED_FILE_CONTEXT_MAX_CHARS


def test_bridge_only_context_remains_owned_by_standard_upload_path(monkeypatch) -> None:
    uploaded = UploadedFileSearchResult(
        file_context="b" * (COMBINED_FILE_CONTEXT_MAX_CHARS + 1),
        file_sources=(),
        errors=(),
    )
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda *_args: uploaded)

    delegated = service_app._delegated_file_context(
        "파일 요약",
        "conversation-1",
        None,
    )

    assert len(delegated[0] or "") == COMBINED_FILE_CONTEXT_MAX_CHARS + 1


def test_chat_answer_returns_typed_error_for_oversized_combined_context(monkeypatch) -> None:
    uploaded = UploadedFileSearchResult(
        file_context="b" * 24_001,
        file_sources=(),
        errors=(),
    )
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda *_args: uploaded)
    client = TestClient(create_app(agent_factory=_echo_factory))

    response = client.post(
        "/chat/answer",
        json={
            "question": "파일 요약",
            "file_context": "d" * DIRECT_FILE_CONTEXT_MAX_CHARS,
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": {
            "code": "input_too_large",
            "field": "combined_file_context",
            "max_chars": COMBINED_FILE_CONTEXT_MAX_CHARS,
        }
    }


def test_f21_and_f59_question_corpora_fit_question_boundary() -> None:
    eval_dir = Path(__file__).resolve().parents[1] / "eval"
    questions: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            question = value.get("question")
            if isinstance(question, str):
                questions.append(question)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for name in ("f21_probe_questions.v1.json", "f59_probe_questions.v1.json"):
        collect(json.loads((eval_dir / name).read_text(encoding="utf-8")))

    assert questions
    assert max(map(len, questions)) <= QUESTION_MAX_CHARS
    assert all(ChatRequest(question=question).question == question for question in questions)
