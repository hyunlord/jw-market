from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import zipfile

from .models import JsonValue, KeywordRow
from .privacy import text_sha256


REPO_ROOT = Path(__file__).resolve().parents[5]
DOCS_DIR = REPO_ROOT / "docs/research/brand_activity/model_cmp"
AUDIT_DIR = DOCS_DIR / "audit"
SCRIPT_DIR = REPO_ROOT / "pipeline/scripts/analysis/brand_activity/model_cmp"
TEST_FILE = REPO_ROOT / "tests/analysis/brand_activity/test_model_cmp.py"
BACKUP_DIR = Path.home() / "jw_artifact_backups/brand_activity_model_cmp"


@dataclass(frozen=True, slots=True)
class ZipResult:
    """Summary of the deliverable zip and permanent backup copy."""

    tmp_zip: Path
    backup_zip: Path
    sha256: str


def write_json(path: Path, payload: JsonValue) -> None:
    """Write deterministic UTF-8 JSON for audit and report inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text while creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def file_sha256(path: Path) -> str:
    """Hash one file for the manifest and final zip evidence."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generated_files() -> list[Path]:
    """Return deliverable files while excluding generated caches and zip packages."""
    paths: list[Path] = []
    for root in (DOCS_DIR, SCRIPT_DIR):
        if root.exists():
            paths.extend(path for path in root.rglob("*") if _include_file(path))
    if TEST_FILE.exists():
        paths.append(TEST_FILE)
    return sorted(paths)


def raw_text_scan(paths: Sequence[Path], rows: Sequence[KeywordRow]) -> dict[str, JsonValue]:
    """Detect exact sampled-source-text leakage in generated docs, audit, or scripts."""
    needles = [
        (text_sha256(row.keyword_text), " ".join(row.keyword_text.split()))
        for row in rows
        if len(" ".join(row.keyword_text.split())) >= 20
    ]
    leaks: list[dict[str, JsonValue]] = []
    for path in paths:
        if path.suffix not in {".md", ".json", ".txt", ".csv", ".py"}:
            continue
        content = _normalized_text(path)
        for source_hash, needle in needles:
            if needle in content:
                leaks.append({"path": _rel(path), "source_text_sha256": source_hash})
    return {
        "scanned_file_count": len(paths),
        "scanned_source_text_count": len(needles),
        "leak_count": len(leaks),
        "leaks": leaks[:20],
    }


def write_manifest(paths: Sequence[Path], output_path: Path) -> None:
    """Write SHA256 manifest for every generated deliverable and script."""
    lines = ["sha256,path"]
    for path in sorted(paths):
        lines.append(f"{file_sha256(path)},{_rel(path)}")
    write_text(output_path, "\n".join(lines) + "\n")


def write_git_status(path: Path) -> None:
    """Record current git status without modifying repository state."""
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    write_text(path, result.stdout)


def create_zip_package(paths: Sequence[Path], *, tag: str) -> ZipResult:
    """Create the requested /tmp zip and a permanent backup copy."""
    tmp_zip = Path("/tmp") / f"brand_activity_model_cmp_{tag}.zip"
    backup_zip = BACKUP_DIR / tmp_zip.name
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, arcname=str(Path(f"brand_activity_model_cmp_{tag}") / _rel(path)))
    shutil.copy2(tmp_zip, backup_zip)
    return ZipResult(tmp_zip=tmp_zip, backup_zip=backup_zip, sha256=file_sha256(tmp_zip))


def _include_file(path: Path) -> bool:
    """Apply audit packaging exclusions requested by the PL brief."""
    if not path.is_file():
        return False
    parts = set(path.parts)
    if "__pycache__" in parts or ".omo" in parts or ".omx" in parts:
        return False
    return path.suffix != ".zip"


def _normalized_text(path: Path) -> str:
    """Read text files in a whitespace-normalized form for leakage scanning."""
    try:
        return " ".join(path.read_text(encoding="utf-8").split())
    except UnicodeDecodeError:
        return ""


def _rel(path: Path) -> str:
    """Render a repository-relative path for manifests and scan output."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
