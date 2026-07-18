"""Fixtures for ingest_hook isolation tests (sqlite ledger + tmp bucket)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ingest_fixtures import FakeTransport

from pipeline.scripts.ingest_hook.ledger import Ledger


@pytest.fixture
def bucket(tmp_path: Path) -> Path:
    root = tmp_path / "bucket"
    root.mkdir()
    return root


@pytest.fixture
def sqlite_ledger(tmp_path: Path) -> Ledger:
    conn = sqlite3.connect(str(tmp_path / "ledger.db"), check_same_thread=False)
    ledger = Ledger(conn, dialect="sqlite")
    ledger.ensure_table()
    return ledger


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()
