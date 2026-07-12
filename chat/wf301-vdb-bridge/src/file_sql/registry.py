"""Server-side ownership registry for logical uploaded-file tables."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from pathlib import Path

from .errors import FileSqlNotFoundError
from .models import TableReference


class UploadSqlRegistry:
    """Mutable process-local registry; physical references never cross the API."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[tuple[str, str], TableReference] = {}
        self._root_dir = Path("/tmp/file_sql")

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def allocate(
        self,
        root_dir: Path,
        session_id: str,
        logical_name: str,
        source_columns: tuple[str, ...],
    ) -> TableReference:
        session_hash = self.session_hash(session_id)
        query_columns = tuple(f"c{index}" for index in range(1, len(source_columns) + 1))
        reference = TableReference(
            session_hash=session_hash,
            logical_name=logical_name,
            database_path=root_dir / session_hash / "session.sqlite3",
            physical_table=f"t_{secrets.token_hex(8)}",
            source_columns=source_columns,
            query_columns=query_columns,
        )
        with self._lock:
            self._tables[(session_hash, logical_name)] = reference
        return reference

    def persist(self, reference: TableReference) -> None:
        registry_dir = reference.database_path.parent / ".registry"
        registry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = registry_dir / self._logical_key(reference.logical_name)
        temporary = path.with_suffix(".tmp")
        payload = {
            "logical_name": reference.logical_name,
            "physical_table": reference.physical_table,
            "source_columns": list(reference.source_columns),
            "query_columns": list(reference.query_columns),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def resolve(self, session_id: str, logical_name: str) -> TableReference:
        key = (self.session_hash(session_id), logical_name)
        with self._lock:
            reference = self._tables.get(key)
        if reference is None:
            reference = self._load_reference(session_id, logical_name)
            with self._lock:
                self._tables[key] = reference
        return reference

    def remove_session(self, session_id: str) -> tuple[TableReference, ...]:
        session_hash = self.session_hash(session_id)
        references = self.references_for_session(session_id)
        with self._lock:
            self._tables = {
                key: value for key, value in self._tables.items() if key[0] != session_hash
            }
        return references

    def remove_logical(self, session_id: str, logical_name: str) -> TableReference:
        reference = self.resolve(session_id, logical_name)
        key = (self.session_hash(session_id), logical_name)
        with self._lock:
            self._tables.pop(key, None)
        registry_path = (
            reference.database_path.parent
            / ".registry"
            / self._logical_key(logical_name)
        )
        registry_path.unlink(missing_ok=True)
        return reference

    def references_for_session(self, session_id: str) -> tuple[TableReference, ...]:
        session_hash = self.session_hash(session_id)
        with self._lock:
            in_memory = tuple(
                reference
                for (owned_hash, _), reference in self._tables.items()
                if owned_hash == session_hash
            )
        if in_memory:
            return in_memory
        registry_dir = self._root_dir / session_hash / ".registry"
        if not registry_dir.is_dir():
            return ()
        references: list[TableReference] = []
        for path in registry_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                logical_name = str(payload["logical_name"])
                references.append(self._load_reference(session_id, logical_name))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(references)

    def bind_root(self, root_dir: Path) -> None:
        self._root_dir = root_dir

    @staticmethod
    def _logical_key(logical_name: str) -> str:
        return hashlib.sha256(logical_name.encode("utf-8")).hexdigest() + ".json"

    def _load_reference(self, session_id: str, logical_name: str) -> TableReference:
        session_hash = self.session_hash(session_id)
        database_path = self._root_dir / session_hash / "session.sqlite3"
        path = database_path.parent / ".registry" / self._logical_key(logical_name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            persisted_logical_name = str(payload["logical_name"])
            physical_table = str(payload["physical_table"])
            source_columns = tuple(str(value) for value in payload["source_columns"])
            query_columns = tuple(str(value) for value in payload["query_columns"])
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FileSqlNotFoundError(
                "logical table is not registered for this session"
            ) from exc
        if persisted_logical_name != logical_name or not re.fullmatch(
            r"t_[0-9a-f]{16}", physical_table
        ):
            raise FileSqlNotFoundError("server-side table registry is invalid")
        return TableReference(
            session_hash=session_hash,
            logical_name=logical_name,
            database_path=database_path,
            physical_table=physical_table,
            source_columns=source_columns,
            query_columns=query_columns,
        )
