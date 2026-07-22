from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import tempfile
import zipfile

from scripts.shadow_transition_contract import (
    OutcomeConsistencyError,
    atomic_write_text,
)


@dataclass(frozen=True, slots=True)
class ArchiveProvenance:
    stale_sha256: str | None
    stale_reason: str | None
    authoritative_sha_location: str
    authoritative_sha256: str
    archive: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_deterministic_zip(source_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in source_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(source_dir).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def finalize_archive(
    source_dir: Path,
    archive_path: Path,
    *,
    rebuild_reason: str | None = None,
) -> ArchiveProvenance:
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)

    stale_sha = _sha256(archive_path) if archive_path.is_file() else None
    if stale_sha is not None and not (rebuild_reason or "").strip():
        raise OutcomeConsistencyError("archive rebuild requires a stale-SHA reason")

    internal_provenance = {
        "stale_sha256": stale_sha,
        "stale_reason": rebuild_reason.strip() if rebuild_reason else None,
        "authoritative_sha_location": "external .zip.sha256 sidecar",
    }
    atomic_write_text(
        source_dir / "archive_build_provenance.json",
        _json_text(internal_provenance),
    )

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{archive_path.name}.",
        dir=archive_path.parent,
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
    try:
        _write_deterministic_zip(source_dir, temporary_path)
        temporary_path.replace(archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    provenance = ArchiveProvenance(
        stale_sha256=stale_sha,
        stale_reason=internal_provenance["stale_reason"],
        authoritative_sha_location=internal_provenance["authoritative_sha_location"],
        authoritative_sha256=_sha256(archive_path),
        archive=archive_path.name,
    )
    atomic_write_text(
        archive_path.with_suffix(archive_path.suffix + ".sha256"),
        f"{provenance.authoritative_sha256}  {archive_path.name}\n",
    )
    atomic_write_text(
        archive_path.with_suffix(archive_path.suffix + ".provenance.json"),
        _json_text(asdict(provenance)),
    )
    return provenance


def _json_text(value: dict[str, str | None]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
