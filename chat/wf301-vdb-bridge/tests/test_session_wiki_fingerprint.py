from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest

from src import ledger, session_wiki


def _documents(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "document_id": index,
            "file_name": name,
            "chunk_count": index + 2,
            "uploaded_at": f"2026-07-10T00:0{index}:00",
        }
        for index, name in enumerate(names, start=1)
    ]


def _page_row(fingerprint: str) -> dict[str, Any]:
    return {
        "page_type": "overview",
        "title": "Session Wiki",
        "md": "compiled",
        "citations": "[]",
        "cost_krw": 0,
        "source_fingerprint": fingerprint,
    }


@dataclass
class _Cursor:
    conn: "_Connection"
    rowcount: int = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        self.conn.executions.append((sql, params))
        if sql.lstrip().startswith("UPDATE session_wiki_page"):
            self.rowcount = self.conn.update_count
            return self.rowcount
        return 0

    def fetchall(self) -> list[dict[str, Any]]:
        _sql, params = self.conn.executions[-1]
        if not self.conn.ready_rows:
            return []
        if len(params) < 4:
            return self.conn.ready_rows
        return [row for row in self.conn.ready_rows if row["source_fingerprint"] == params[-1]]

    def fetchone(self) -> dict[str, Any]:
        return {"acquired": 1}


@dataclass
class _Connection:
    ready_rows: list[dict[str, Any]] = field(default_factory=list)
    update_count: int = 0
    executions: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    commits: int = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_source_fingerprint_changes_when_active_document_membership_changes() -> None:
    first = _documents("brief.docx", "evidence.pdf")
    added = [*first, _documents("metrics.xlsx")[0] | {"document_id": 3}]
    deleted = added[1:]

    assert session_wiki.source_fingerprint(first) != session_wiki.source_fingerprint(added)
    assert session_wiki.source_fingerprint(added) != session_wiki.source_fingerprint(deleted)
    assert session_wiki.source_fingerprint(first) == session_wiki.source_fingerprint(list(reversed(first)))


def test_read_ready_pages_excludes_page_compiled_for_old_fingerprint(monkeypatch) -> None:
    before = _documents("brief.docx", "evidence.pdf")
    current = [*before, _documents("metrics.xlsx")[0] | {"document_id": 3}]
    conn = _Connection(ready_rows=[_page_row(session_wiki.source_fingerprint(before))])
    monkeypatch.setattr(session_wiki, "ensure_schema", lambda _conn: None)

    pages = session_wiki.read_ready_pages(conn, 301, "session-1", current)

    assert pages == []


def test_mark_pages_stale_uses_existing_expired_status_without_commit(monkeypatch) -> None:
    conn = _Connection(update_count=1)
    monkeypatch.setattr(session_wiki, "ensure_schema", lambda _conn: None)

    updated = session_wiki.mark_pages_stale(conn, 301, "session-1")

    sql, params = conn.executions[-1]
    assert "SET status=%s" in sql
    assert params == ("expired", 301, "session-1", "ready")
    assert updated == 1
    assert conn.commits == 0


def test_compile_scope_recompiles_when_ready_page_fingerprint_is_stale(monkeypatch) -> None:
    before = _documents("brief.docx", "evidence.pdf")
    current = [*before, _documents("metrics.xlsx")[0] | {"document_id": 3}]
    conn = _Connection(ready_rows=[_page_row(session_wiki.source_fingerprint(before))])
    compiled: list[list[dict[str, Any]]] = []

    @contextmanager
    def connection() -> Iterator[_Connection]:
        yield conn

    monkeypatch.setattr(ledger, "ledger_connection", connection)
    monkeypatch.setattr(ledger, "list_session_documents", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(session_wiki, "ensure_schema", lambda _conn: None)
    monkeypatch.setattr(session_wiki, "_acquire_lock", lambda *_args: True)
    monkeypatch.setattr(session_wiki, "_release_lock", lambda *_args: None)
    monkeypatch.setattr(session_wiki, "_load_chunks", lambda _ids: [{"text": "source"}])
    monkeypatch.setattr(session_wiki, "_compile_pages", lambda *_args: [session_wiki.WikiPage("overview", "Wiki", "fresh", ())])
    monkeypatch.setattr(session_wiki, "_upsert_pages", lambda _conn, _wf, _sid, documents, _pages: compiled.append(documents))

    session_wiki.compile_scope(301, "session-1")

    assert compiled == [current]


def test_failed_recompile_does_not_expose_stale_page(monkeypatch) -> None:
    before = _documents("brief.docx", "evidence.pdf")
    current = [*before, _documents("metrics.xlsx")[0] | {"document_id": 3}]
    conn = _Connection(ready_rows=[_page_row(session_wiki.source_fingerprint(before))])

    @contextmanager
    def connection() -> Iterator[_Connection]:
        yield conn

    monkeypatch.setattr(ledger, "ledger_connection", connection)
    monkeypatch.setattr(ledger, "list_session_documents", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(session_wiki, "ensure_schema", lambda _conn: None)
    monkeypatch.setattr(session_wiki, "_acquire_lock", lambda *_args: True)
    monkeypatch.setattr(session_wiki, "_release_lock", lambda *_args: None)
    monkeypatch.setattr(session_wiki, "_load_chunks", lambda _ids: [{"text": "source"}])

    def fail_compile(*_args: Any) -> list[session_wiki.WikiPage]:
        raise RuntimeError("compile failed")

    monkeypatch.setattr(session_wiki, "_compile_pages", fail_compile)

    with pytest.raises(RuntimeError, match="compile failed"):
        session_wiki.compile_scope(301, "session-1")

    assert session_wiki.read_ready_pages(conn, 301, "session-1", current) == []


def test_xlsx_only_session_does_not_trigger_wiki() -> None:
    assert session_wiki.should_trigger(_documents("one.xlsx", "two.xlsx")) is False
