"""Durable test-load state, deliberately separate from the production ledger."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class TestRunRecord:
    run_id: str
    category: str
    epoch: str
    manifest_sha: str
    manifest_path: str
    requested_by: str
    status: str
    created_at: str
    updated_at: str
    job_name: str | None = None
    current_stage: str | None = None
    stages: tuple[dict, ...] = ()
    result: dict | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["stages"] = list(self.stages)
        return payload


class ActiveTestRunError(RuntimeError):
    pass


class TestRunStore:
    """Atomic JSON records on the result PVC; no MariaDB/shared-DB writes."""

    __test__ = False

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        lock_path = self.root / ".store.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(
        self,
        *,
        category: str,
        epoch: str,
        manifest_sha: str,
        manifest_path: str,
        requested_by: str,
    ) -> TestRunRecord:
        with self.locked():
            if self.active_for_category(category):
                raise ActiveTestRunError(
                    f"test load already active for category={category}"
                )
            now = datetime.now(timezone.utc).isoformat()
            record = TestRunRecord(
                run_id=str(uuid.uuid4()),
                category=category,
                epoch=epoch,
                manifest_sha=manifest_sha,
                manifest_path=manifest_path,
                requested_by=requested_by,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            self._write(record)
            return record

    def get(self, run_id: str) -> TestRunRecord | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["stages"] = tuple(payload.get("stages") or ())
        return TestRunRecord(**payload)

    def update(self, run_id: str, **changes) -> TestRunRecord:
        with self.locked():
            current = self.get(run_id)
            if current is None:
                raise KeyError(run_id)
            payload = current.as_dict()
            payload.update(changes)
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            payload["stages"] = tuple(payload.get("stages") or ())
            record = TestRunRecord(**payload)
            self._write(record)
            return record

    def active_for_category(self, category: str) -> tuple[TestRunRecord, ...]:
        rows = []
        for path in sorted(self.root.glob("*.json")):
            record = self.get(path.stem)
            if (
                record is not None
                and record.category == category
                and record.status in ACTIVE_STATUSES
            ):
                rows.append(record)
        return tuple(rows)

    def _path(self, run_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(run_id))
        except ValueError as exc:
            raise ValueError("invalid test run id") from exc
        return self.root / f"{canonical}.json"

    def _write(self, record: TestRunRecord) -> None:
        path = self._path(record.run_id)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.root, prefix=f".{record.run_id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    record.as_dict(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
