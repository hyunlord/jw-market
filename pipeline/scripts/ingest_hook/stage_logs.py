"""Durable, secret-safe stage log storage and paging."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")
_SENSITIVE_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_JSON_ASSIGNMENT = re.compile(
    r"""(?i)(["'](?:password|passwd|secret|token|api[_-]?key|access[_-]?key)["']"""
    r"""\s*:\s*["'])([^"']*)(["'])"""
)
_CLI_ASSIGNMENT = re.compile(
    r"(?i)(--(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(?:=|\s+))([^\s]+)"
)
_BEARER = re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)([^\s]+)")
_URI_CREDENTIAL = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE)
MAX_READABLE_LOG_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LogPage:
    text: str
    total_bytes: int
    next_offset: int
    truncated: bool


def _safe_name(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def job_log_dir(root: Path, *, job_name: str) -> Path:
    return Path(root) / _safe_name(job_name, "job_name")


def full_log_path(root: Path, *, job_name: str) -> Path:
    return job_log_dir(root, job_name=job_name) / "full.log"


def expired_marker_path(root: Path, *, job_name: str) -> Path:
    """Return the explicit tombstone used by any future retention worker."""
    return job_log_dir(root, job_name=job_name) / ".expired"


def missing_log_reason(root: Path, *, job_name: str) -> str:
    """Classify absence without guessing that a never-written log expired."""
    return (
        "log_expired"
        if expired_marker_path(root, job_name=job_name).is_file()
        else "log_not_preserved"
    )


def stage_log_path(root: Path, *, job_name: str, stage: str) -> Path:
    return (
        job_log_dir(root, job_name=job_name)
        / "stages"
        / f"{_safe_name(stage, 'stage')}.log"
    )


def ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


def redact(text: str) -> str:
    redacted = text
    for name, value in os.environ.items():
        if value and len(value) >= 4 and _SENSITIVE_NAME.search(name):
            redacted = redacted.replace(value, "[REDACTED]")
    redacted = _JSON_ASSIGNMENT.sub(r"\1[REDACTED]\3", redacted)
    redacted = _CLI_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGNMENT.sub(r"\1\2[REDACTED]", redacted)
    redacted = _BEARER.sub(r"\1[REDACTED]", redacted)
    return _URI_CREDENTIAL.sub(r"\1[REDACTED]\3", redacted)


def read_log_page(
    root: Path,
    *,
    job_name: str,
    stage: str | None,
    offset: int = 0,
    limit: int = 64 * 1024,
) -> LogPage:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1 or limit > 256 * 1024:
        raise ValueError("limit must be between 1 and 262144")
    path = (
        stage_log_path(root, job_name=job_name, stage=stage)
        if stage
        else full_log_path(root, job_name=job_name)
    )
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > MAX_READABLE_LOG_BYTES:
        raise ValueError("log file exceeds readable size limit")

    # Redact before paging so a secret split at a page boundary cannot leak.
    payload = redact(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
    total = len(payload)
    if offset > total:
        offset = total
    try:
        payload[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("offset must be a UTF-8 character boundary") from exc

    end = min(offset + limit, total)
    while end < total:
        try:
            chunk = payload[offset:end]
            text = chunk.decode("utf-8")
            break
        except UnicodeDecodeError:
            end += 1
    else:
        chunk = payload[offset:end]
        text = chunk.decode("utf-8")
    next_offset = end
    return LogPage(
        text=text,
        total_bytes=total,
        next_offset=next_offset,
        truncated=next_offset < total,
    )
