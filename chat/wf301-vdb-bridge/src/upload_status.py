"""Session-owned persistent status for asynchronous upload processing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import secrets
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal, Sequence


UploadJobState = Literal[
    "accepted",
    "preprocessing",
    "committing",
    "ready",
    "blocked",
    "failed",
    "interrupted",
    "expired",
]

_UPLOAD_ID_PATTERN = re.compile(r"^upl_[A-Za-z0-9_-]{16,64}$")
_VALID_STATES = frozenset(
    {
        "accepted",
        "preprocessing",
        "committing",
        "ready",
        "blocked",
        "failed",
        "interrupted",
        "expired",
    }
)
_TERMINAL_STATES = frozenset({"ready", "blocked", "failed", "interrupted", "expired"})
_INTERRUPTED_MESSAGE = "파일 처리가 중단되었습니다. 다시 업로드해 주세요."


class UploadJobNotFoundError(LookupError):
    """Raised without revealing whether another session owns an upload job."""


@dataclass(frozen=True, slots=True)
class UploadWorksheetCard:
    name: str
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class UploadFileCard:
    file_name: str
    file_type: str
    size_bytes: int
    title: str | None = None
    sheet_count: int | None = None
    sheets: tuple[UploadWorksheetCard, ...] = ()
    page_count: int | None = None
    slide_count: int | None = None


@dataclass(frozen=True, slots=True)
class UploadFileStatus:
    file_name: str
    state: UploadJobState = "accepted"
    route: str | None = None
    message: str | None = None
    card: UploadFileCard | None = None


@dataclass(frozen=True, slots=True)
class UploadJobStatus:
    upload_id: str
    workflow_id: int
    state: UploadJobState
    files: tuple[UploadFileStatus, ...]
    message: str | None
    updated_at: datetime
    expires_at: datetime

    @property
    def ready(self) -> bool:
        return self.state == "ready"


class UploadStatusRegistry:
    """Persist upload status beneath a hashed session directory."""

    def __init__(
        self,
        root_dir: Path,
        *,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self._root_dir = root_dir
        self._stale_after = stale_after
        self._lock = threading.RLock()

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def session_root(self, session_id: str) -> Path:
        return self._root_dir / self.session_hash(session_id)

    def status_path(self, session_id: str, upload_id: str) -> Path:
        self._validate_upload_id(upload_id)
        return self.session_root(session_id) / ".upload_jobs" / f"{upload_id}.json"

    def create(
        self,
        *,
        session_id: str,
        workflow_id: int,
        file_names: Sequence[str],
        file_cards: Sequence[UploadFileCard] = (),
        expires_at: datetime,
        now: datetime | None = None,
    ) -> UploadJobStatus:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        upload_id = f"upl_{secrets.token_urlsafe(18)}"
        cards_by_name = {card.file_name: card for card in file_cards}
        status = UploadJobStatus(
            upload_id=upload_id,
            workflow_id=workflow_id,
            state="accepted",
            files=tuple(
                UploadFileStatus(
                    file_name=Path(file_name).name,
                    card=cards_by_name.get(Path(file_name).name),
                )
                for file_name in file_names
            ),
            message=None,
            updated_at=timestamp,
            expires_at=expires_at.astimezone(UTC),
        )
        with self._guard(session_id):
            self._write(session_id, status)
        return status

    def transition(
        self,
        *,
        session_id: str,
        workflow_id: int,
        upload_id: str,
        state: UploadJobState,
        files: Sequence[UploadFileStatus] | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> UploadJobStatus:
        if state not in _VALID_STATES:
            raise ValueError("invalid upload job state")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._guard(session_id):
            current = self._load(session_id, workflow_id, upload_id)
            next_files = tuple(files) if files is not None else current.files
            if files is not None:
                cards_by_name = {
                    item.file_name: item.card
                    for item in current.files
                    if item.card is not None
                }
                next_files = tuple(
                    item
                    if item.card is not None
                    else replace(item, card=cards_by_name.get(item.file_name))
                    for item in next_files
                )
            updated = replace(
                current,
                state=state,
                files=next_files,
                message=message,
                updated_at=timestamp,
            )
            self._write(session_id, updated)
            return updated

    def resolve(
        self,
        *,
        session_id: str,
        workflow_id: int,
        upload_id: str,
        now: datetime | None = None,
    ) -> UploadJobStatus:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._guard(session_id):
            status = self._load(session_id, workflow_id, upload_id)
            if status.expires_at <= timestamp:
                expired = replace(
                    status,
                    state="expired",
                    message="업로드 상태 보관 기간이 만료되었습니다.",
                    updated_at=timestamp,
                )
                self._write(session_id, expired)
                return expired
            if (
                status.state not in _TERMINAL_STATES
                and timestamp - status.updated_at > self._stale_after
            ):
                interrupted = replace(
                    status,
                    state="interrupted",
                    message=_INTERRUPTED_MESSAGE,
                    updated_at=timestamp,
                )
                self._write(session_id, interrupted)
                return interrupted
            return status

    @contextmanager
    def _guard(self, session_id: str) -> Iterator[None]:
        with self._lock:
            directory = self.session_root(session_id) / ".upload_jobs"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = directory / ".jobs.lock"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(
        self,
        session_id: str,
        workflow_id: int,
        upload_id: str,
    ) -> UploadJobStatus:
        path = self.status_path(session_id, upload_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            state = str(payload["state"])
            if state not in _VALID_STATES:
                raise ValueError("invalid state")
            status = UploadJobStatus(
                upload_id=str(payload["upload_id"]),
                workflow_id=int(payload["workflow_id"]),
                state=state,  # type: ignore[arg-type]
                files=tuple(
                    UploadFileStatus(
                        file_name=str(item["file_name"]),
                        state=str(item["state"]),  # type: ignore[arg-type]
                        route=str(item["route"]) if item.get("route") is not None else None,
                        message=(
                            str(item["message"])
                            if item.get("message") is not None
                            else None
                        ),
                        card=self._parse_card(item.get("card")),
                    )
                    for item in payload["files"]
                ),
                message=(
                    str(payload["message"])
                    if payload.get("message") is not None
                    else None
                ),
                updated_at=datetime.fromisoformat(str(payload["updated_at"])).astimezone(UTC),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])).astimezone(UTC),
            )
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise UploadJobNotFoundError("upload job is not registered") from exc
        if status.upload_id != upload_id or status.workflow_id != workflow_id:
            raise UploadJobNotFoundError("upload job is not registered")
        if any(item.file_name != Path(item.file_name).name for item in status.files):
            raise UploadJobNotFoundError("upload job is not registered")
        if any(
            item.card is not None and item.card.file_name != item.file_name
            for item in status.files
        ):
            raise UploadJobNotFoundError("upload job is not registered")
        return status

    @staticmethod
    def _parse_card(value: object) -> UploadFileCard | None:
        if not isinstance(value, dict):
            return None
        sheets_value = value.get("sheets")
        sheets = tuple(
            UploadWorksheetCard(
                name=str(item["name"]),
                row_count=int(item["row_count"]) if item.get("row_count") is not None else None,
                column_count=(
                    int(item["column_count"])
                    if item.get("column_count") is not None
                    else None
                ),
            )
            for item in sheets_value
            if isinstance(item, dict) and item.get("name") is not None
        ) if isinstance(sheets_value, list) else ()
        return UploadFileCard(
            file_name=str(value["file_name"]),
            file_type=str(value["file_type"]),
            size_bytes=int(value.get("size_bytes") or 0),
            title=str(value["title"]) if value.get("title") is not None else None,
            sheet_count=(
                int(value["sheet_count"])
                if value.get("sheet_count") is not None
                else None
            ),
            sheets=sheets,
            page_count=(
                int(value["page_count"])
                if value.get("page_count") is not None
                else None
            ),
            slide_count=(
                int(value["slide_count"])
                if value.get("slide_count") is not None
                else None
            ),
        )

    def _write(self, session_id: str, status: UploadJobStatus) -> None:
        path = self.status_path(session_id, status.upload_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        payload = asdict(status)
        payload["updated_at"] = status.updated_at.isoformat()
        payload["expires_at"] = status.expires_at.isoformat()
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _validate_upload_id(upload_id: str) -> None:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise UploadJobNotFoundError("upload job is not registered")
