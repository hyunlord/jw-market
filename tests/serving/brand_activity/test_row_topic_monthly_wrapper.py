from __future__ import annotations

from pathlib import Path
from types import TracebackType

import pytest

from pipeline.scripts.deploy.brand_activity_307 import row_topic_monthly_wrapper as wrapper


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.sql = sql

    def fetchall(self) -> list[dict[str, str]]:
        return [{"run_id": "pending-run"}]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_no_pending_receipt_is_a_noop_without_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no complete pending axis receipt, the monthly assignment never starts."""
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(wrapper, "_pending_topic_set_versions", lambda: ())
    monkeypatch.setattr(
        wrapper,
        "_run_row_topic",
        lambda *_args, **_kwargs: pytest.fail("assignment must not run"),
    )
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0


def test_pending_receipt_query_uses_exact_fail_closed_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a durable pending receipt, the operational query discovers only ready runs."""
    cursor = _Cursor()
    monkeypatch.setattr(wrapper, "_connect", lambda: _Connection(cursor))

    assert wrapper._pending_topic_set_versions() == ("pending-run",)
    assert "axis_status='complete'" in cursor.sql
    assert "assignment_status IN ('pending','running','gap')" in cursor.sql


def test_pending_receipt_is_reconciled_even_when_no_calls_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given an artificial pending receipt, the wrapper discovers and reconciles it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        wrapper,
        "_pending_topic_set_versions",
        lambda: ("pending-run",),
    )

    def _run(mode: str, version: str, max_calls: int | None = None):
        del max_calls
        calls.append((mode, version))
        if mode == "dry-run":
            return {"pending_rows": 0, "pending_batches": 0}
        return {"complete": True}

    monkeypatch.setattr(wrapper, "_run_row_topic", _run)
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0
    assert calls == [
        ("dry-run", "pending-run"),
        ("reconcile", "pending-run"),
    ]
