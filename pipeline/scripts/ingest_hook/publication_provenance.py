"""Fail-closed attestation for an atomic serving mart publication."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


_FULL_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_IMMUTABLE_IMAGE: Final = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class PublicationProvenanceError(RuntimeError):
    """The running image cannot produce trustworthy publication evidence."""


@dataclass(frozen=True, slots=True)
class PublicationProvenance:
    builder_commit: str
    image_digest: str
    published_at_utc: str
    target_db: str
    tables: tuple[str, ...]


def build_publication_provenance(
    *,
    target_db: str,
    tables: tuple[str, ...],
    builder_commit: str | None = None,
    image_digest: str | None = None,
) -> PublicationProvenance:
    """Attest publication using only identity owned by the running image."""

    app_version = os.environ.get("APP_VERSION", "").strip().lower()
    if not app_version:
        raise PublicationProvenanceError(
            "APP_VERSION is required for mart publication provenance"
        )
    if not _FULL_GIT_SHA.fullmatch(app_version):
        raise PublicationProvenanceError(
            "APP_VERSION must be a full 40-character git commit SHA"
        )
    supplied_commit = (builder_commit or "").strip().lower()
    if supplied_commit and supplied_commit != app_version:
        raise PublicationProvenanceError(
            f"builder_commit {supplied_commit} does not match APP_VERSION {app_version}"
        )
    resolved_image = (
        image_digest or os.environ.get("INGEST_JOB_IMAGE") or ""
    ).strip()
    if not _IMMUTABLE_IMAGE.fullmatch(resolved_image):
        raise PublicationProvenanceError(
            "immutable INGEST_JOB_IMAGE digest is required for mart publication"
        )
    if not target_db or not tables:
        raise PublicationProvenanceError(
            "publication target database and tables are required"
        )
    return PublicationProvenance(
        builder_commit=app_version,
        image_digest=resolved_image,
        published_at_utc=datetime.now(timezone.utc).isoformat(),
        target_db=target_db,
        tables=tables,
    )


def record_publication_provenance(
    journal_path: Path,
    provenance: PublicationProvenance,
) -> None:
    """Atomically append attestation to the existing activation journal."""

    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if payload.get("target_db") != provenance.target_db:
        raise PublicationProvenanceError(
            "activation journal target does not match publication target"
        )
    if tuple(payload.get("tables") or ()) != provenance.tables:
        raise PublicationProvenanceError(
            "activation journal tables do not match publication tables"
        )
    payload["publication_provenance"] = asdict(provenance)
    temp_path = journal_path.with_suffix(journal_path.suffix + ".provenance.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    with temp_path.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(journal_path)
    directory_fd = os.open(
        journal_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
