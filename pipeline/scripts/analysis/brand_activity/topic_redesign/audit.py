"""Sanitized audit, scan, manifest, and zip packaging helpers."""

from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from .models import CoverageRow, JsonValue, LabelCandidate, MessageRow, MethodScore
from .reports import pct, write_json
from .text import normalize_text, text_sha256


REPO_ROOT = Path(__file__).resolve().parents[5]
DOCS_DIR = REPO_ROOT / "docs/research/brand_activity/topic_redesign"
AUDIT_ROOT = DOCS_DIR / "audit"
SCRIPT_DIR = REPO_ROOT / "pipeline/scripts/analysis/brand_activity/topic_redesign"
TEST_FILE = REPO_ROOT / "tests/analysis/brand_activity/test_topic_redesign.py"


def write_audit_artifacts(
    audit_dir: Path,
    db_payload: dict[str, JsonValue | list[MessageRow]],
    method_scores: list[MethodScore],
    candidates: list[LabelCandidate],
    coverage: list[CoverageRow],
    residual: dict[str, list[tuple[str, int]]],
    assignments: dict[str, tuple[str, ...]],
) -> None:
    """Write sanitized machine-readable audit artifacts."""
    write_json(audit_dir / "db_snapshot.json", _snapshot_payload(db_payload))
    write_json(audit_dir / "embedding_model_info.json", embedding_model_info())
    write_json(audit_dir / "residual_terms.json", {market: [[term, count] for term, count in terms] for market, terms in residual.items()})
    write_json(audit_dir / "assignment_counts.json", assignment_counts(assignments))
    write_method_csv(audit_dir / "method_scores.csv", method_scores)
    write_candidates_csv(audit_dir / "label_candidates_redacted.csv", candidates)
    write_coverage_csv(audit_dir / "coverage_by_market.csv", coverage)
    write_git_status(audit_dir / "git_status.txt")


def run_scans(audit_dir: Path, env: dict[str, str], keyword_rows: list[MessageRow]) -> dict[str, JsonValue]:
    """Run secret-value and raw-machine-artifact scans."""
    files = generated_files(include_markdown=True, audit_dir=audit_dir)
    secret_matches = scan_secret_values(files, env)
    raw_matches = scan_raw_text_machine_artifacts([*audit_dir.rglob("*"), DOCS_DIR / "REDESIGN_03_DICTIONARY_DRAFT.json"], keyword_rows)
    return {
        "secret_plaintext_status": "NO_MATCH" if not secret_matches else "MATCH",
        "secret_matches": secret_matches,
        "machine_raw_text_status": "NO_MATCH" if not raw_matches else "MATCH",
        "machine_raw_text_matches": raw_matches[:10],
        "markdown_review_snippets": "PRESENT_BY_DESIGN_IN_REDESIGN_02_ONLY",
    }


def write_findings(
    audit_dir: Path,
    generated_at: str,
    candidates: list[LabelCandidate],
    coverage: list[CoverageRow],
    method_scores: list[MethodScore],
    scan_payload: dict[str, JsonValue],
) -> None:
    """Write a compact audit finding summary."""
    avg_unmatched = sum(row.unmatched_rate for row in coverage) / len(coverage)
    weighted_unmatched = sum(row.unmatched_rows for row in coverage) / sum(row.rows for row in coverage)
    best_methods = sorted({score.method: score.score for score in method_scores}.items(), key=lambda item: -item[1])
    lines = [
        "# FINDINGS",
        "",
        f"- Generated: {generated_at}",
        "- Scope: read-only local topic redesign PoC; no DB writes, deploy, push, or external LLM calls.",
        "- Recommended extraction method: hybrid n-gram + PMI collocation + TF-IDF/SVD cluster discovery anchored to market dictionaries.",
        f"- Candidate labels: {len(candidates)} across 17 ATC4 markets.",
        f"- Draft unmatched rate: {pct(avg_unmatched)} simple mean, {pct(weighted_unmatched)} row-weighted.",
        f"- Method score order: {', '.join(method for method, _ in best_methods)}.",
        f"- Secret scan: {scan_payload['secret_plaintext_status']}; machine raw-text scan: {scan_payload['machine_raw_text_status']}.",
        "- Next gate: PL/marketing label merge/rename confirmation and manual evaluation-set labeling.",
    ]
    (audit_dir / "FINDINGS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(audit_dir: Path) -> list[dict[str, JsonValue]]:
    """Write SHA256 manifest for generated deliverables and scripts."""
    rows: list[dict[str, JsonValue]] = []
    for path in generated_files(include_markdown=True, audit_dir=audit_dir):
        if path.name == "manifest_sha256.csv":
            continue
        rows.append({"path": str(path.relative_to(REPO_ROOT)), "bytes": path.stat().st_size, "sha256": file_sha256(path)})
    with (audit_dir / "manifest_sha256.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "bytes", "sha256"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def create_zip_package(tag: str, audit_dir: Path) -> tuple[Path, str]:
    """Create the requested zip in /tmp and return its SHA256."""
    zip_path = Path("/tmp") / f"topic_redesign_{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in generated_files(include_markdown=True, audit_dir=audit_dir):
            archive.write(path, path.relative_to(REPO_ROOT))
    digest = file_sha256(zip_path)
    zip_path.with_suffix(zip_path.suffix + ".sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, digest


def backup_zip(zip_path: Path, zip_sha: str, tag: str) -> Path:
    """Copy the final zip to a non-/tmp backup path."""
    # 백업 위치는 환경변수 JW_BACKUP_DIR 로 지정(미설정 시 홈 밑 ~/jw_backups). 하드코딩 로컬경로 제거.
    backup_dir = Path(os.environ.get("JW_BACKUP_DIR", str(Path.home() / "jw_backups"))) / "topic_redesign"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"topic_redesign_{tag}.zip"
    shutil.copy2(zip_path, backup_path)
    backup_path.with_suffix(backup_path.suffix + ".sha256").write_text(f"{zip_sha}  {backup_path.name}\n", encoding="utf-8")
    return backup_path


def run_summary(
    tag: str,
    generated_at: str,
    candidates: list[LabelCandidate],
    coverage: list[CoverageRow],
    method_scores: list[MethodScore],
    scan_payload: dict[str, JsonValue],
    manifest_rows: list[dict[str, JsonValue]],
    zip_path: Path,
    zip_sha: str,
    backup_path: Path,
) -> dict[str, JsonValue]:
    """Build final no-secret summary for console and audit."""
    avg_unmatched = sum(row.unmatched_rate for row in coverage) / len(coverage)
    weighted_unmatched = sum(row.unmatched_rows for row in coverage) / sum(row.rows for row in coverage)
    best_method = max(method_scores, key=lambda item: item.score).method if method_scores else "n/a"
    return {
        "tag": tag,
        "generated_at": generated_at,
        "best_extraction_method": "hybrid n-gram + PMI collocation + TF-IDF/SVD cluster discovery anchored to dictionaries",
        "best_single_score_surface": best_method,
        "markets": len(coverage),
        "label_candidate_count": len(candidates),
        "average_unmatched_rate": round(avg_unmatched, 4),
        "weighted_unmatched_rate": round(weighted_unmatched, 4),
        "small_markets": [row.market for row in coverage if row.rows < 80],
        "open_questions": 0,
        "secret_scan": scan_payload["secret_plaintext_status"],
        "machine_raw_text_scan": scan_payload["machine_raw_text_status"],
        "manifest_entries": len(manifest_rows),
        "zip_path": str(zip_path),
        "zip_sha256": zip_sha,
        "permanent_backup_zip": str(backup_path),
    }


def _snapshot_payload(db_payload: dict[str, JsonValue | list[MessageRow]]) -> dict[str, JsonValue]:
    """Serialize DB evidence while excluding source message text."""
    return {
        "columns": db_payload["columns"],
        "before_snapshot": db_payload["before_snapshot"],
        "after_snapshot": db_payload["after_snapshot"],
        "read_only_equal": bool(db_payload["read_only_equal"]),
        "keyword_rows_read": len(db_payload["keyword_rows"]) if isinstance(db_payload["keyword_rows"], list) else 0,
        "auxiliary_rows_read": len(db_payload["auxiliary_rows"]) if isinstance(db_payload["auxiliary_rows"], list) else 0,
    }


def embedding_model_info() -> dict[str, JsonValue]:
    """Record local clustering fallback and uncalled neural candidates."""
    return {
        "executed": {"method": "TF-IDF + TruncatedSVD + KMeans", "runtime": "local Mac Python", "external_llm_calls": 0, "neural_embedding_downloads": 0, "reason": "신경망 임베딩은 discovery 보조라 로컬 통계 PoC 수행"},
        "not_called_candidates": [
            {"model": "multilingual-e5-large", "license": "MIT", "status": "design candidate only"},
            {"model": "bge-m3", "license": "MIT", "status": "design candidate only"},
            {"model": "ko-sbert-nli", "license": "license review required", "status": "design candidate only"},
        ],
    }


def assignment_counts(assignments: dict[str, tuple[str, ...]]) -> dict[str, JsonValue]:
    """Count assigned labels without row-level raw outputs."""
    counts: defaultdict[str, int] = defaultdict(int)
    for labels in assignments.values():
        counts["UNMATCHED" if not labels else "|".join(labels)] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def write_method_csv(path: Path, scores: list[MethodScore]) -> None:
    """Write method comparison metrics as CSV."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["market", "method", "candidate_count", "coverage_rate", "noise_rate", "redundancy_rate", "score", "top_candidates", "note"])
        for score in scores:
            writer.writerow([score.market, score.method, score.candidate_count, score.coverage_rate, score.noise_rate, score.redundancy_rate, score.score, "; ".join(score.top_candidates), score.note])


def write_candidates_csv(path: Path, candidates: list[LabelCandidate]) -> None:
    """Write candidate labels without representative raw text."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["market", "label", "hit_count", "coverage_rate", "source", "keywords", "evidence_terms", "note"])
        for candidate in candidates:
            writer.writerow([candidate.market, candidate.label, candidate.hit_count, candidate.coverage_rate, candidate.source, "; ".join(candidate.keywords), "; ".join(candidate.evidence_terms), candidate.note])


def write_coverage_csv(path: Path, coverage: list[CoverageRow]) -> None:
    """Write coverage metrics as CSV."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["market", "rows", "matched_rows", "unmatched_rows", "multilabel_rows", "unmatched_rate", "multilabel_rate"])
        for row in coverage:
            writer.writerow([row.market, row.rows, row.matched_rows, row.unmatched_rows, row.multilabel_rows, row.unmatched_rate, row.multilabel_rate])


def write_git_status(path: Path) -> None:
    """Capture read-only git status without staging or committing."""
    result = subprocess.run(["git", "status", "--short", "--branch"], cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    path.write_text(result.stdout, encoding="utf-8")


def generated_files(include_markdown: bool, audit_dir: Path | None = None) -> list[Path]:
    """List generated files while excluding caches and zip artifacts."""
    suffixes = {".py", ".json", ".csv", ".txt", ".log", ".md"} if include_markdown else {".json", ".csv", ".txt", ".log"}
    files: list[Path] = []
    if DOCS_DIR.exists():
        for path in DOCS_DIR.iterdir():
            if path.is_file() and not excluded_from_package(path) and path.suffix.lower() in suffixes:
                files.append(path)
    if audit_dir is not None and audit_dir.exists():
        for path in audit_dir.rglob("*"):
            if path.is_file() and not excluded_from_package(path) and path.suffix.lower() in suffixes:
                files.append(path)
    if SCRIPT_DIR.exists():
        for path in SCRIPT_DIR.rglob("*"):
            if path.is_file() and not excluded_from_package(path) and path.suffix.lower() in suffixes:
                files.append(path)
    if TEST_FILE.exists():
        files.append(TEST_FILE)
    return sorted(dict.fromkeys(files))


def excluded_from_package(path: Path) -> bool:
    """Return true for cache/internal/archive artifacts that must not be packaged."""
    parts = set(path.parts)
    return "__pycache__" in parts or ".omo" in parts or ".omx" in parts or path.suffix in {".pyc", ".zip"}


def scan_secret_values(paths: list[Path], env: dict[str, str]) -> list[dict[str, str]]:
    """Search generated files for credential values without logging the values."""
    private_key_marker = "BEGIN " + "OPENSSH PRIVATE KEY"
    passphrase_markers = ("GCP_KEY_" + "PASS=", "BASTION_" + "PASS=")
    secret_values = [(key, value) for key, value in env.items() if len(value) >= 8 and ("PASS" in key or "KEY" in key or "SECRET" in key)]
    matches: list[dict[str, str]] = []
    for path in paths:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if private_key_marker in content or any(marker in content for marker in passphrase_markers):
            matches.append({"file": str(path.relative_to(REPO_ROOT)), "kind": "private_key_or_passphrase_literal"})
        for key, value in secret_values:
            if value and value in content:
                matches.append({"file": str(path.relative_to(REPO_ROOT)), "kind": key})
    return matches


def scan_raw_text_machine_artifacts(paths: list[Path], keyword_rows: list[MessageRow]) -> list[dict[str, str]]:
    """Ensure JSON/CSV/TXT audit artifacts do not contain full source messages."""
    source_texts = {normalize_text(row.text) for row in keyword_rows if len(normalize_text(row.text)) >= 24}
    matches: list[dict[str, str]] = []
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {".json", ".csv", ".txt", ".log"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for text in source_texts:
            if text in content:
                matches.append({"file": str(path.relative_to(REPO_ROOT)), "message_hash": text_sha256(text)})
                break
    return matches


def file_sha256(path: Path) -> str:
    """Hash a file in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
