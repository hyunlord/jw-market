from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.cache_deep_analysis_events_update import (
    CacheEventsUpdateError,
    connect_db,
    get_events,
    quote_ident,
    strip_events_from_raw,
)


def test_strip_events_preserves_non_event_payload() -> None:
    raw = (
        '{"brand":"악템라","data":{"events":[{"id":"e1"}],'
        '"forecast":{"ok":true},"simulation":{"ok":true},"ai_analysis":{"ok":true}}}'
    )

    result = strip_events_from_raw(raw)

    assert "events" not in result["data"]
    assert result["data"]["forecast"] == {"ok": True}
    assert result["data"]["simulation"] == {"ok": True}
    assert result["data"]["ai_analysis"] == {"ok": True}


def test_get_events_returns_only_list() -> None:
    assert get_events({"data": {"events": [{"id": "e1"}]}}) == [{"id": "e1"}]
    assert get_events({"data": {"events": {"id": "bad"}}}) == []
    assert get_events({"data": {}}) == []


def test_quote_ident_rejects_unsafe_table_name() -> None:
    with pytest.raises(CacheEventsUpdateError):
        quote_ident("cache_deep_analysis;DROP")


def test_connect_db_refuses_non_d2_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart")
    monkeypatch.setenv("MARIADB_HOST", "example.invalid")
    monkeypatch.setenv("D2_WRITER_USER", "writer")
    monkeypatch.setenv("D2_WRITER_PASSWORD", "secret")

    with pytest.raises(CacheEventsUpdateError, match="refusing to write non-d2 database"):
        connect_db()
