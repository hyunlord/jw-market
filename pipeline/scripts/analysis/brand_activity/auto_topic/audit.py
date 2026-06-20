from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import csv
import hashlib
import json
import shutil
import subprocess
import zipfile

from .models import JsonValue, KeywordRow
from .privacy import text_sha256


REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DOCS_DIR = REPO_ROOT / "docs/research/brand_activity/auto_topic"
DEFAULT_AUDIT_DIR = DEFAULT_DOCS_DIR / "audit"
SCRIPT_DIR = REPO_ROOT / "pipeline/scripts/analysis/brand_activity/auto_topic"
TEST_DIR = REPO_ROOT / "tests/analysis/brand_activity"
BACKUP_DIR = Path.home() / "jw_artifact_backups/brand_activity_auto_topic"


@dataclass(frozen=True, slots=True)
class ZipResult:
    """Summary of the requested /tmp zip and permanent backup."""

    tmp_zip: Path
    backup_zip: Path
    sha256: str


def write_json(path: Path, value: JsonValue) -> None:
    """Write deterministic UTF-8 JSON for audit and report inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    """Write UTF-8 text while creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def file_sha256(path: Path) -> str:
    """Hash one file in streaming mode for manifests and zip evidence."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generated_files(docs_dir: Path, audit_dir: Path) -> list[Path]:
    """Return packageable files while excluding caches, zips, and internal OMX files."""
    files: list[Path] = []
    docs_root = docs_dir.resolve()
    if docs_root.exists():
        files.extend(path.resolve() for path in docs_root.rglob("*") if _include_file(path) and "audit" not in path.relative_to(docs_root).parts)
    for root in (audit_dir.resolve(), SCRIPT_DIR.resolve()):
        if root.exists():
            files.extend(path.resolve() for path in root.rglob("*") if _include_file(path))
    for path in TEST_DIR.glob("test_auto_topic*.py"):
        if _include_file(path):
            files.append(path.resolve())
    return sorted({path for path in files})


def raw_text_scan(paths: Sequence[Path], rows: Sequence[KeywordRow]) -> dict[str, JsonValue]:
    """Detect exact sampled-source-text leakage in generated artifacts."""
    needles = [(text_sha256(row.keyword_text), " ".join(row.keyword_text.split())) for row in rows if len(" ".join(row.keyword_text.split())) >= 24]
    leaks: list[dict[str, JsonValue]] = []
    for path in paths:
        if path.suffix.lower() not in {".md", ".html", ".json", ".csv", ".txt", ".py"}:
            continue
        content = _normalized_text(path)
        for source_hash, text in needles:
            if text in content:
                leaks.append({"path": _rel(path), "source_text_sha256": source_hash})
                break
    return {"status": "NO_MATCH" if not leaks else "MATCH", "scanned_file_count": len(paths), "scanned_source_text_count": len(needles), "leak_count": len(leaks), "leaks": leaks[:20]}


def write_manifest(paths: Sequence[Path], output_path: Path) -> list[dict[str, JsonValue]]:
    """Write SHA256 manifest rows for generated deliverables and scripts."""
    rows = [{"path": _rel(path), "bytes": path.stat().st_size, "sha256": file_sha256(path)} for path in sorted(paths) if path.name != "manifest_sha256.csv"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "bytes", "sha256"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_git_status(path: Path) -> None:
    """Record git status for audit without changing repository state."""
    result = subprocess.run(["git", "status", "--short", "--branch"], cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    write_text(path, result.stdout)


def create_zip_package(paths: Sequence[Path], *, tag: str) -> ZipResult:
    """Create the requested /tmp zip and copy it to a permanent backup directory."""
    tmp_zip = Path("/tmp") / f"brand_activity_auto_topic_{tag}.zip"
    backup_zip = BACKUP_DIR / tmp_zip.name
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, arcname=str(Path(f"brand_activity_auto_topic_{tag}") / _rel(path)))
    shutil.copy2(tmp_zip, backup_zip)
    sha256 = file_sha256(tmp_zip)
    tmp_zip.with_suffix(tmp_zip.suffix + ".sha256").write_text(f"{sha256}  {tmp_zip}\n", encoding="utf-8")
    backup_zip.with_suffix(backup_zip.suffix + ".sha256").write_text(f"{sha256}  {backup_zip}\n", encoding="utf-8")
    return ZipResult(tmp_zip=tmp_zip, backup_zip=backup_zip, sha256=sha256)


def _include_file(path: Path) -> bool:
    """Apply PL packaging exclusions for pycache, zips, and hidden runtime folders."""
    if not path.is_file():
        return False
    parts = set(path.parts)
    if "__pycache__" in parts or ".omo" in parts or ".omx" in parts or ".omc" in parts:
        return False
    return path.suffix.lower() not in {".pyc", ".zip"}


def _normalized_text(path: Path) -> str:
    """Read a text file with whitespace collapsed for exact leak scanning."""
    try:
        return " ".join(path.read_text(encoding="utf-8").split())
    except UnicodeDecodeError:
        return ""


def _rel(path: Path) -> str:
    """Render a path relative to the repository when possible."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
