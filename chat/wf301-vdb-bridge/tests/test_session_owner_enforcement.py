from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import main
from src.session_ownership import SessionOwnershipRegistry


def test_controlled_two_actor_session_is_hidden_from_non_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(main, "_SESSION_OWNERSHIP", SessionOwnershipRegistry(tmp_path))
    monkeypatch.setattr(main, "_legacy_actor_uid", lambda _session_id: None)
    monkeypatch.setattr(main, "_session_has_documents", lambda _session_id, _workflow_id: False)

    main._claim_upload_session("dummy-session", 301, "42")
    assert main._require_session_owner("dummy-session", "42") == "genos-user:42"

    with pytest.raises(HTTPException) as denied:
        main._require_session_owner("dummy-session", "84")
    assert denied.value.status_code == 404
    assert denied.value.detail == "session not found"


def test_legacy_session_migration_requires_the_ledger_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(main, "_SESSION_OWNERSHIP", SessionOwnershipRegistry(tmp_path))
    monkeypatch.setattr(
        main,
        "_legacy_actor_uid",
        lambda _session_id: "genos-user:42",
    )

    with pytest.raises(HTTPException) as denied:
        main._require_session_owner("legacy-session", "84")
    assert denied.value.status_code == 404

    assert main._require_session_owner("legacy-session", "42") == "genos-user:42"


def test_unowned_legacy_session_with_documents_cannot_be_claimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(main, "_SESSION_OWNERSHIP", SessionOwnershipRegistry(tmp_path))
    monkeypatch.setattr(main, "_legacy_actor_uid", lambda _session_id: None)
    monkeypatch.setattr(main, "_session_has_documents", lambda _session_id, _workflow_id: True)

    with pytest.raises(HTTPException) as denied:
        main._claim_upload_session("legacy-with-documents", 301, "42")

    assert denied.value.status_code == 404
    assert denied.value.detail == "session not found"


def test_legacy_claim_probe_includes_expired_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(main.ledger, "ledger_connection", lambda: nullcontext(object()))

    def list_documents(_conn, **kwargs):
        observed.update(kwargs)
        return [{"document_id": 1}]

    monkeypatch.setattr(main.ledger, "list_session_documents", list_documents)

    assert main._session_has_documents("legacy-session", 301) is True
    assert observed == {
        "workflow_id": 301,
        "session_id": "legacy-session",
        "include_expired": True,
    }
