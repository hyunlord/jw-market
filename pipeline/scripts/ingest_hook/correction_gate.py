"""Optional fail-closed gate for corrected copies of published inputs.

The gate is deliberately disabled by default. With
``REQUIRE_CORRECTION_REJECT_GATE=0``, a same-name file with changed bytes can
still reach the loaders' historical filename/row deduplication and be ignored.
Enabling the flag compares the submitted content SHA against the latest
durable publication inventory before any loader starts.
"""
from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Final, Iterable

from pipeline.scripts.ingest_hook.config import ENV_REQUIRE_CORRECTION_REJECT_GATE
from pipeline.scripts.ingest_hook.contract import Manifest


# The contracts currently share a file-level correction identity. Keeping the
# mapping explicit prevents a future source from silently inheriting it.
LOGICAL_IDENTITY_FIELDS: Final = {
    "ubist": ("category", "epoch", "basename"),
    "iqvia_nsa": ("category", "epoch", "basename"),
    "iqvia_csd_channel": ("category", "epoch", "basename"),
    "iqvia_csd_keyword": ("category", "epoch", "basename"),
}


@dataclass(frozen=True, slots=True)
class StoredFileRevision:
    logical_identity: str
    sha256: str
    loaded_at: str


@dataclass(frozen=True, slots=True)
class CorrectionConflict:
    logical_identity: str
    filename: str
    period: str
    previous_sha256: str
    submitted_sha256: str
    previous_loaded_at: str


@dataclass(frozen=True, slots=True)
class CorrectionGateResult:
    status: str
    conflicts: tuple[CorrectionConflict, ...] = ()


class CorrectionRejected(RuntimeError):
    """The same logical file/period was published with different bytes."""

    def __init__(self, conflicts: tuple[CorrectionConflict, ...]):
        self.conflicts = conflicts
        first = conflicts[0]
        super().__init__(
            "정정본 충돌: "
            f"파일={first.filename}, 기간={first.period}, "
            f"이전 적재 시각={first.previous_loaded_at}. "
            "같은 논리 파일·기간에 다른 내용이 이미 적재되어 있습니다. "
            "기존 정본을 덮어쓰지 말고 승인된 정정 절차를 이용해 주세요."
        )


RevisionReader = Callable[[Manifest], Iterable[StoredFileRevision]]


def gate_enabled() -> bool:
    """Only the explicit value ``1`` activates the policy."""
    return os.environ.get(ENV_REQUIRE_CORRECTION_REJECT_GATE, "0").strip() == "1"


def logical_file_identity(category: str, epoch: str, path: str) -> str:
    if category not in LOGICAL_IDENTITY_FIELDS:
        raise ValueError(f"correction gate has no identity contract for {category!r}")
    basename = unicodedata.normalize(
        "NFC", PurePosixPath(path.replace("\\", "/")).name
    ).casefold()
    return "\x1f".join((category, epoch, basename))


def assess(
    manifest: Manifest,
    stored_revisions: Iterable[StoredFileRevision],
) -> CorrectionGateResult:
    """Compare current files to the newest prior revision of each logical key."""
    newest: dict[str, StoredFileRevision] = {}
    for revision in stored_revisions:
        newest.setdefault(revision.logical_identity, revision)

    conflicts: list[CorrectionConflict] = []
    matched = 0
    for item in manifest.files:
        identity = logical_file_identity(
            manifest.category,
            manifest.epoch,
            item.path,
        )
        previous = newest.get(identity)
        if previous is None:
            continue
        matched += 1
        if previous.sha256 != item.sha256:
            conflicts.append(
                CorrectionConflict(
                    logical_identity=identity,
                    filename=PurePosixPath(item.path.replace("\\", "/")).name,
                    period=manifest.epoch,
                    previous_sha256=previous.sha256,
                    submitted_sha256=item.sha256,
                    previous_loaded_at=previous.loaded_at,
                )
            )
    if conflicts:
        raise CorrectionRejected(tuple(conflicts))
    if matched == len(manifest.files):
        return CorrectionGateResult("noop")
    return CorrectionGateResult("new")


def read_stored_revisions(manifest: Manifest) -> tuple[StoredFileRevision, ...]:
    """Read prior successful publication inventories without mutating the DB."""
    from pipeline.scripts.ingest_hook import config, publication_signal

    connection = config.open_mart_connection()
    try:
        cursor = connection.cursor()
        mark = publication_signal._parameter_marker(connection)
        table = publication_signal._provenance_table()
        cursor.execute(
            f"SELECT input_inventory_json, published_at_utc "
            f"FROM `{table}` WHERE category={mark} AND epoch={mark} "
            "ORDER BY mart_publication_epoch DESC",
            (manifest.category, manifest.epoch),
        )
        rows = cursor.fetchall()
    finally:
        connection.close()

    revisions: list[StoredFileRevision] = []
    for row in rows:
        if isinstance(row, dict):
            inventory_json = row["input_inventory_json"]
            loaded_at = row["published_at_utc"]
        else:
            inventory_json, loaded_at = row
        inventory = json.loads(str(inventory_json))
        if not isinstance(inventory, list):
            raise RuntimeError("publication provenance inventory is not a list")
        for item in inventory:
            if not isinstance(item, dict):
                raise RuntimeError("publication provenance file entry is not an object")
            path = str(item.get("path") or "")
            sha256 = str(item.get("sha256") or "").lower()
            if not path or len(sha256) != 64:
                raise RuntimeError("publication provenance file identity is incomplete")
            revisions.append(
                StoredFileRevision(
                    logical_identity=logical_file_identity(
                        manifest.category,
                        manifest.epoch,
                        path,
                    ),
                    sha256=sha256,
                    loaded_at=str(loaded_at),
                )
            )
    return tuple(revisions)


def enforce(
    manifest: Manifest,
    *,
    revision_reader: RevisionReader = read_stored_revisions,
) -> CorrectionGateResult:
    if not gate_enabled():
        return CorrectionGateResult("disabled")
    if manifest.category not in LOGICAL_IDENTITY_FIELDS:
        return CorrectionGateResult("not_applicable")
    result = assess(manifest, revision_reader(manifest))
    print(
        "gate=correction_reject "
        f"status={result.status} category={manifest.category} epoch={manifest.epoch}"
    )
    return result
