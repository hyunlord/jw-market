from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.session_ownership import (
    SessionNotFoundError,
    SessionOwnershipRegistry,
    normalize_actor_uid,
)
from src.main import _upload_session_id


def test_actor_uid_requires_a_positive_numeric_portal_user_id() -> None:
    assert normalize_actor_uid("42") == "genos-user:42"

    for invalid in (None, "", "0", "-1", "email@example.com"):
        with pytest.raises(ValueError, match="positive integer"):
            normalize_actor_uid(invalid)


def test_upload_session_id_preserves_supplied_values_or_issues_a_random_id() -> None:
    assert _upload_session_id("app-session", None) == "app-session"
    assert _upload_session_id("app-session", "chat-session") == "chat-session"

    first = _upload_session_id(None, None)
    second = _upload_session_id(None, None)
    assert first != second
    assert len(first) == 32
    assert all(character in "0123456789abcdef" for character in first)


def test_new_session_owner_is_immutable_and_mismatch_is_not_found(tmp_path: Path) -> None:
    registry = SessionOwnershipRegistry(tmp_path)

    registry.claim_new("session-a", "genos-user:42")
    registry.require_owner("session-a", "genos-user:42")

    with pytest.raises(SessionNotFoundError, match="session not found"):
        registry.require_owner("session-a", "genos-user:84")
    with pytest.raises(SessionNotFoundError, match="session not found"):
        registry.claim_new("session-a", "genos-user:84")


def test_registry_persists_only_hashed_session_key_and_actor_uid(tmp_path: Path) -> None:
    registry = SessionOwnershipRegistry(tmp_path)
    registry.claim_new("private-session-id", "genos-user:42")

    records = list((tmp_path / ".session-owners").glob("*.json"))
    assert len(records) == 1
    assert "private-session-id" not in records[0].name
    assert json.loads(records[0].read_text()) == {"actor_uid": "genos-user:42"}
    assert records[0].stat().st_mode & 0o777 == 0o600


def test_legacy_owner_can_be_migrated_but_cannot_be_claimed_by_another_actor(
    tmp_path: Path,
) -> None:
    registry = SessionOwnershipRegistry(tmp_path)

    registry.require_owner(
        "legacy-session",
        "genos-user:42",
        legacy_actor_uid="genos-user:42",
    )
    registry.require_owner("legacy-session", "genos-user:42")

    with pytest.raises(SessionNotFoundError, match="session not found"):
        registry.require_owner(
            "other-legacy-session",
            "genos-user:84",
            legacy_actor_uid="genos-user:42",
        )
