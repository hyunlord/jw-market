from __future__ import annotations

from jw_chat_agent_poc.service.actor_context import actor_user_scope
from jw_chat_agent_poc.service.file_search_client import (
    fetch_uploaded_file_schema_columns,
    has_active_uploaded_file,
    search_uploaded_files,
)
from jw_chat_agent_poc.service.file_sql_query import (
    SqlFileSource,
    _fetch_schema,
    _run_query,
)


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def test_file_search_and_documents_forward_the_trusted_portal_actor(monkeypatch) -> None:
    observed: list[dict[str, str]] = []

    def post(*_args, **kwargs):
        observed.append(kwargs["headers"])
        return _Response({"document_count": 1, "file_context": "context"})

    def get(*_args, **kwargs):
        observed.append(kwargs["headers"])
        return _Response({"documents": [{"file_name": "probe.pdf"}]})

    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.post", post)
    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.get", get)

    with actor_user_scope(42):
        assert search_uploaded_files("question", "session-a") is not None
        assert has_active_uploaded_file("session-a") is True

    assert observed == [
        {"X-Portal-User-Id": "42"},
        {"X-Portal-User-Id": "42"},
    ]


def test_file_sql_schema_and_query_forward_the_trusted_portal_actor(monkeypatch) -> None:
    observed: list[dict[str, str]] = []

    def post(*_args, **kwargs):
        observed.append(kwargs["headers"])
        if len(observed) == 1:
            return _Response({"columns": []})
        return _Response({"columns": ["value"], "rows": [[1]]})

    monkeypatch.setattr("jw_chat_agent_poc.service.file_sql_query.requests.post", post)
    source = SqlFileSource(
        logical_name="table_a",
        file_name="probe.xlsx",
        sheet_name="Sheet1",
        row_count=1,
        column_count=1,
    )

    with actor_user_scope(42):
        _fetch_schema(source, "session-a")
        _run_query("session-a", "table_a", "SELECT value FROM data")

    assert observed == [
        {"X-Portal-User-Id": "42"},
        {"X-Portal-User-Id": "42"},
    ]


def test_file_schema_probe_forwards_the_trusted_portal_actor(monkeypatch) -> None:
    observed: list[dict[str, str]] = []

    def post(*_args, **kwargs):
        observed.append(kwargs["headers"])
        return _Response(
            {
                "sql_sources": [
                    {
                        "logical_name": "table_a",
                        "file_name": "probe.xlsx",
                        "sheet_name": "Sheet1",
                    }
                ]
            }
        )

    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.post", post)
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.fetch_sql_schema_columns",
        lambda *_args: ("value",),
    )

    with actor_user_scope(42):
        assert fetch_uploaded_file_schema_columns("session-a") == ("value",)

    assert observed == [{"X-Portal-User-Id": "42"}]
