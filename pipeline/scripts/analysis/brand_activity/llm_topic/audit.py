from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from .models import JsonValue, KeywordRow


REPO_ROOT = Path(__file__).resolve().parents[5]
DOCS_DIR = REPO_ROOT / "docs/research/brand_activity/llm_topic"
AUDIT_ROOT = DOCS_DIR / "audit"
SCRIPT_DIR = REPO_ROOT / "pipeline/scripts/analysis/brand_activity/llm_topic"
TEST_FILE = REPO_ROOT / "tests/analysis/brand_activity/test_llm_topic_poc.py"


def write_json(path: Path, value: JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def generated_files(audit_dir: Path) -> list[Path]:
    suffixes = {".md", ".json", ".csv", ".txt", ".py"}
    files: list[Path] = []
    if DOCS_DIR.exists():
        files.extend(path for path in DOCS_DIR.iterdir() if path.is_file() and path.suffix.lower() in suffixes)
    if audit_dir.exists():
        files.extend(path for path in audit_dir.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)
    if SCRIPT_DIR.exists():
        files.extend(path for path in SCRIPT_DIR.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)
    if TEST_FILE.exists():
        files.append(TEST_FILE)
    return sorted({path for path in files if not _excluded(path)})


def write_manifest(audit_dir: Path) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    for path in generated_files(audit_dir):
        if path.name == "manifest_sha256.csv":
            continue
        rows.append({"path": str(path.relative_to(REPO_ROOT)), "bytes": path.stat().st_size, "sha256": file_sha256(path)})
    with (audit_dir / "manifest_sha256.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "bytes", "sha256"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def create_zip_package(tag: str, audit_dir: Path) -> tuple[Path, str, Path]:
    zip_path = Path("/tmp") / f"llm_topic_poc_{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in generated_files(audit_dir):
            archive.write(path, path.relative_to(REPO_ROOT))
    zip_sha = file_sha256(zip_path)
    zip_path.with_suffix(zip_path.suffix + ".sha256").write_text(f"{zip_sha}  {zip_path.name}\n", encoding="utf-8")
    # 백업 위치는 환경변수 JW_BACKUP_DIR 로 지정(미설정 시 홈 밑 ~/jw_backups). 하드코딩 로컬경로 제거.
    backup_dir = Path(os.environ.get("JW_BACKUP_DIR", str(Path.home() / "jw_backups"))) / "llm_topic"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / zip_path.name
    shutil.copy2(zip_path, backup_path)
    backup_path.with_suffix(backup_path.suffix + ".sha256").write_text(f"{zip_sha}  {backup_path.name}\n", encoding="utf-8")
    return zip_path, zip_sha, backup_path


def raw_text_scan(paths: list[Path], rows: list[KeywordRow]) -> dict[str, JsonValue]:
    source_texts = {" ".join(row.keyword_text.split()) for row in rows if len(" ".join(row.keyword_text.split())) >= 24}
    matches: list[dict[str, str]] = []
    for path in paths:
        if path.suffix.lower() not in {".json", ".csv", ".txt", ".log"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for text in source_texts:
            if text and text in content:
                matches.append({"file": str(path.relative_to(REPO_ROOT)), "message_hash": text_sha256(text)})
                break
    return {"status": "NO_MATCH" if not matches else "MATCH", "matches": matches[:20]}


def write_git_status(path: Path) -> None:
    result = subprocess.run(["git", "status", "--short", "--branch"], cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    path.write_text(result.stdout, encoding="utf-8")


def _excluded(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" in parts or ".omo" in parts or ".omx" in parts or path.suffix.lower() in {".pyc", ".zip"}
