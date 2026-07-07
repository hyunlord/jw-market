from __future__ import annotations

from jw_chat_agent_poc.service import file_search_client


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_search_uploaded_files_forwards_conversation_as_session(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Response(
            {
                "file_context": "wiki context",
                "file_sources": [{"file_name": "guide.pdf"}],
                "errors": [],
            }
        )

    monkeypatch.setenv("JW_CHAT_FILE_SEARCH_BASE", "http://files")
    monkeypatch.setenv("JW_CHAT_FILE_WORKFLOW_ID", "301")
    monkeypatch.setattr(file_search_client.requests, "post", fake_post)

    result = file_search_client.search_uploaded_files("질문", "conv-1")

    assert result is not None
    assert result.file_context == "wiki context"
    assert result.file_sources == ("guide.pdf",)
    assert calls == [
        (
            "http://files/search",
            {
                "workflow_id": 301,
                "app_session_id": "conv-1",
                "chat_id": "conv-1",
                "question": "질문",
            },
            3.0,
        )
    ]


def test_search_uploaded_files_silent_when_bridge_returns_empty(monkeypatch):
    def fake_post(url, json, timeout):
        return _Response({"file_context": "", "file_sources": []})

    monkeypatch.setattr(file_search_client.requests, "post", fake_post)

    assert file_search_client.search_uploaded_files("질문", "conv-1") is None
