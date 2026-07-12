"""Server-owned temporary upload metadata and session confinement."""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Sequence


class TempDocumentNotFoundError(LookupError):
    """Raised without disclosing whether another session owns a temp document."""


@dataclass(frozen=True, slots=True)
class OwnedTempDocument:
    workflow_id: int
    temp_document_id: int
    file_name: str
    file_path: Path
    expires_at: datetime


class UploadOwnershipRegistry:
    """Persist immutable upload metadata under a hashed session root."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._root_dir = root_dir
        self._lock = threading.RLock()

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def session_root(self, root_dir: Path, session_id: str) -> Path:
        return root_dir / self.session_hash(session_id)

    def metadata_path(self, root_dir: Path, session_id: str, temp_document_id: int) -> Path:
        return self.session_root(root_dir, session_id) / ".ownership" / f"{temp_document_id}.json"

    def register(
        self,
        *,
        root_dir: Path,
        session_id: str,
        workflow_id: int,
        temp_document_id: int,
        file_name: str,
        file_path: Path,
        expires_at: datetime,
    ) -> OwnedTempDocument:
        self._root_dir = root_dir
        session_root = self.session_root(root_dir, session_id)
        canonical_path = self._confined_file(session_root, file_path)
        owned = OwnedTempDocument(
            workflow_id=workflow_id,
            temp_document_id=temp_document_id,
            file_name=Path(file_name).name,
            file_path=canonical_path,
            expires_at=expires_at.astimezone(UTC),
        )
        metadata_path = self.metadata_path(root_dir, session_id, temp_document_id)
        metadata_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = metadata_path.with_suffix(".tmp")
        payload = {
            "workflow_id": owned.workflow_id,
            "temp_document_id": owned.temp_document_id,
            "file_name": owned.file_name,
            "file_path": str(owned.file_path),
            "expires_at": owned.expires_at.isoformat(),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(metadata_path)
        return owned

    def resolve_many(
        self,
        session_id: str,
        workflow_id: int,
        temp_document_ids: Sequence[int],
    ) -> tuple[OwnedTempDocument, ...]:
        root_dir = self._required_root()
        with self._lock:
            # Resolve the complete set before returning anything so mixed-owner
            # requests cannot partially enter the commit pipeline.
            return tuple(
                self._load(root_dir, session_id, workflow_id, temp_document_id)
                for temp_document_id in temp_document_ids
            )

    @contextmanager
    def commit_guard(
        self,
        session_id: str,
        workflow_id: int,
        temp_document_ids: Sequence[int],
    ) -> Iterator[tuple[OwnedTempDocument, ...]]:
        with self._lock:
            root_dir = self._required_root()
            lock_dir = self.session_root(root_dir, session_id) / ".ownership"
            lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = lock_dir / ".commit.lock"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    owned = self.resolve_many(session_id, workflow_id, temp_document_ids)
                    yield owned
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def remove(self, session_id: str, temp_document_id: int) -> None:
        root_dir = self._required_root()
        self.metadata_path(root_dir, session_id, temp_document_id).unlink(missing_ok=True)

    def _required_root(self) -> Path:
        if self._root_dir is None:
            raise TempDocumentNotFoundError("temporary document is not registered")
        return self._root_dir

    def _load(
        self,
        root_dir: Path,
        session_id: str,
        workflow_id: int,
        temp_document_id: int,
    ) -> OwnedTempDocument:
        metadata_path = self.metadata_path(root_dir, session_id, temp_document_id)
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            owned = OwnedTempDocument(
                workflow_id=int(payload["workflow_id"]),
                temp_document_id=int(payload["temp_document_id"]),
                file_name=str(payload["file_name"]),
                file_path=Path(str(payload["file_path"])),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])).astimezone(UTC),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TempDocumentNotFoundError("temporary document is not registered") from exc
        if (
            owned.workflow_id != workflow_id
            or owned.temp_document_id != temp_document_id
            or owned.expires_at <= datetime.now(UTC)
            or owned.file_name != Path(owned.file_name).name
        ):
            raise TempDocumentNotFoundError("temporary document is not registered")
        session_root = self.session_root(root_dir, session_id)
        canonical_path = self._confined_file(session_root, owned.file_path)
        return OwnedTempDocument(
            workflow_id=owned.workflow_id,
            temp_document_id=owned.temp_document_id,
            file_name=owned.file_name,
            file_path=canonical_path,
            expires_at=owned.expires_at,
        )

    @staticmethod
    def _confined_file(session_root: Path, file_path: Path) -> Path:
        try:
            if file_path.is_symlink():
                raise ValueError("symlink is not allowed")
            root = session_root.resolve(strict=True)
            canonical = file_path.resolve(strict=True)
            canonical.relative_to(root)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise TempDocumentNotFoundError("temporary document is not registered") from exc
        if not canonical.is_file():
            raise TempDocumentNotFoundError("temporary document is not registered")
        return canonical
