"""Immutable actor ownership for code-serving document sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


class SessionNotFoundError(LookupError):
    pass


def normalize_actor_uid(raw_user_id: str | None) -> str:
    try:
        user_id = int(str(raw_user_id or "").strip())
    except ValueError as exc:
        raise ValueError("X-Portal-User-Id must be a positive integer") from exc
    if user_id <= 0:
        raise ValueError("X-Portal-User-Id must be a positive integer")
    return f"genos-user:{user_id}"


class SessionOwnershipRegistry:
    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir / ".session-owners"

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _record_path(self, session_id: str) -> Path:
        return self._root / f"{self.session_hash(session_id)}.json"

    def _read_owner(self, session_id: str) -> str | None:
        path = self._record_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionNotFoundError("session not found") from exc
        actor_uid = payload.get("actor_uid") if isinstance(payload, dict) else None
        if not isinstance(actor_uid, str) or not actor_uid:
            raise SessionNotFoundError("session not found")
        return actor_uid

    def is_registered(self, session_id: str) -> bool:
        return self._read_owner(session_id) is not None

    def _create(self, session_id: str, actor_uid: str) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._record_path(session_id)
        lock_path = path.with_suffix(".lock")
        payload = json.dumps(
            {"actor_uid": actor_uid},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with lock_path.open("a+b") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if path.exists():
                return
            temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)

    def claim_new(self, session_id: str, actor_uid: str) -> None:
        self._create(session_id, actor_uid)
        if self._read_owner(session_id) != actor_uid:
            raise SessionNotFoundError("session not found")

    def require_owner(
        self,
        session_id: str,
        actor_uid: str,
        *,
        legacy_actor_uid: str | None = None,
    ) -> None:
        owner = self._read_owner(session_id)
        if owner is None and legacy_actor_uid == actor_uid:
            self._create(session_id, actor_uid)
            owner = self._read_owner(session_id)
        if owner != actor_uid:
            raise SessionNotFoundError("session not found")
