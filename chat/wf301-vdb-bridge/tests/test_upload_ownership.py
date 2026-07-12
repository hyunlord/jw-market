from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import main
from src.models import BridgeRequest, TempDocument
from src.upload_ownership import (
    TempDocumentNotFoundError,
    UploadOwnershipRegistry,
)


def _register(
    registry: UploadOwnershipRegistry,
    root: Path,
    session_id: str,
    temp_document_id: int,
    *,
    file_name: str = "owned.xlsx",
):
    session_root = registry.session_root(root, session_id)
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"TEMP_DOCUMENT_{temp_document_id}.xlsx"
    path.write_bytes(b"owned")
    return registry.register(
        root_dir=root,
        session_id=session_id,
        workflow_id=301,
        temp_document_id=temp_document_id,
        file_name=file_name,
        file_path=path,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_same_owner_resolves_server_metadata(tmp_path: Path) -> None:
    registry = UploadOwnershipRegistry()
    owned = _register(registry, tmp_path, "session-a", 11)

    resolved = registry.resolve_many("session-a", 301, [11])

    assert resolved == (owned,)
    assert resolved[0].file_name == "owned.xlsx"


def test_wrong_owner_is_not_disclosed(tmp_path: Path) -> None:
    registry = UploadOwnershipRegistry()
    _register(registry, tmp_path, "session-a", 11)

    with pytest.raises(TempDocumentNotFoundError):
        registry.resolve_many("session-b", 301, [11])


def test_mixed_owner_request_is_rejected_before_returning_any_item(tmp_path: Path) -> None:
    registry = UploadOwnershipRegistry()
    _register(registry, tmp_path, "session-a", 11)
    _register(registry, tmp_path, "session-b", 22)

    with pytest.raises(TempDocumentNotFoundError):
        registry.resolve_many("session-a", 301, [11, 22])


def test_caller_path_and_filename_cannot_override_ledger(tmp_path: Path) -> None:
    registry = UploadOwnershipRegistry()
    owned = _register(registry, tmp_path, "session-a", 11)
    forged = tmp_path / "forged.xlsx"
    forged.write_bytes(b"forged")

    resolved = registry.resolve_many("session-a", 301, [11])

    assert resolved[0].file_name == owned.file_name
    assert resolved[0].file_path == owned.file_path
    assert resolved[0].file_path != forged


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_path_confinement_rejects_escape(tmp_path: Path, kind: str) -> None:
    registry = UploadOwnershipRegistry()
    owned = _register(registry, tmp_path, "session-a", 11)
    metadata_path = registry.metadata_path(tmp_path, "session-a", 11)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    outside = tmp_path.parent / "outside-owned-test.xlsx"
    outside.write_bytes(b"outside")
    try:
        if kind == "traversal":
            payload["file_path"] = str(owned.file_path.parent / ".." / outside.name)
        else:
            link = owned.file_path.parent / "linked.xlsx"
            link.symlink_to(outside)
            payload["file_path"] = str(link)
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(TempDocumentNotFoundError):
            registry.resolve_many("session-a", 301, [11])
    finally:
        outside.unlink(missing_ok=True)


def test_expired_temp_document_is_rejected(tmp_path: Path) -> None:
    registry = UploadOwnershipRegistry()
    owned = _register(registry, tmp_path, "session-a", 11)
    metadata_path = registry.metadata_path(tmp_path, "session-a", 11)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TempDocumentNotFoundError):
        registry.resolve_many("session-a", owned.workflow_id, [11])


def test_commit_guard_serializes_concurrent_commits(tmp_path: Path) -> None:
    first_registry = UploadOwnershipRegistry()
    _register(first_registry, tmp_path, "session-a", 11)
    second_registry = UploadOwnershipRegistry(tmp_path)
    entered: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with first_registry.commit_guard("session-a", 301, [11]):
            entered.append("first")
            first_entered.set()
            release_first.wait(timeout=2)

    def second() -> None:
        first_entered.wait(timeout=2)
        with second_registry.commit_guard("session-a", 301, [11]):
            entered.append("second")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert entered == ["first"]
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert entered == ["first", "second"]


def test_bridge_request_rejects_forged_filename_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = UploadOwnershipRegistry()
    _register(registry, tmp_path, "session-a", 11)
    monkeypatch.setattr(main, "_UPLOAD_OWNERSHIP", registry)
    request = BridgeRequest(
        workflow_id=301,
        vdb_id=139,
        app_session_id="session-a",
        temp_documents=[
            TempDocument(
                temp_document_id=11,
                file_name="forged.xlsx",
                file_path="/etc/passwd",
            )
        ],
    )

    with pytest.raises(HTTPException) as captured:
        main._owned_bridge_request(request, request_id="test-request")

    assert captured.value.status_code == 404


def test_bridge_request_returns_404_for_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = UploadOwnershipRegistry()
    _register(registry, tmp_path, "session-a", 11)
    monkeypatch.setattr(main, "_UPLOAD_OWNERSHIP", registry)
    request = BridgeRequest(
        workflow_id=301,
        vdb_id=139,
        app_session_id="session-b",
        temp_documents=[TempDocument(temp_document_id=11, file_name="owned.xlsx")],
    )

    with pytest.raises(HTTPException) as captured:
        main._owned_bridge_request(request, request_id="test-request")

    assert captured.value.status_code == 404
