"""Drift gate for the mart DB generation name.

``pipeline/mart_config.py`` is the single Python source of truth for the mart
generation. Kubernetes manifests and standalone scripts keep deliberate pinned
copies (fail-closed guards / crawl-image layout); this gate keeps every copy
equal to the canonical constant so a generation switch cannot be applied
halfway.
"""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.mart_config import DEFAULT_MART_DB_NAME, DEFAULT_SOURCE_EPOCH, resolve_mart_db_name

REPO_ROOT = Path(__file__).resolve().parents[2]

# Any mart d2 generation literal (the generation being canonicalized).
GENERATION_PATTERN = re.compile(r"\bjw_mart_d2_stage_[0-9a-z_]+")

SCAN_DIRS = ("pipeline", "deploy")
SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".sh"}

# Python files allowed to carry the literal itself. Standalone scripts run
# outside the package context in the crawl image layout, so they keep a local
# pinned default instead of importing pipeline.mart_config.
ALLOWED_PY_LITERAL_FILES = {
    "pipeline/mart_config.py",
    "pipeline/scripts/crawler/tier2_body_match_runner.py",
    "pipeline/scripts/crawler/crawl_retention.py",
    "pipeline/scripts/crawler/crawl_2tier.py",
    "pipeline/scripts/crawler/tier2_full_scoring_runner.py",
    "pipeline/scripts/crawler/tier2_catalog.py",
    "pipeline/scripts/ai_analysis/agent2_variant_candidate_builder.py",
    "pipeline/scripts/ai_analysis/stage3a7_create_and_insert_ai_analysis.py",
    "pipeline/scripts/ai_analysis/agent2_variant_promotion.py",
    "pipeline/scripts/ai_analysis/agent2_variant_repair.py",
}


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_DIRS:
        root = REPO_ROOT / base
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in SCAN_SUFFIXES:
                files.append(path)
    return files


def test_every_generation_literal_matches_canonical() -> None:
    mismatches: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in GENERATION_PATTERN.finditer(text):
            if match.group(0) != DEFAULT_MART_DB_NAME:
                line = text.count("\n", 0, match.start()) + 1
                mismatches.append(f"{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)}")
    assert not mismatches, (
        "Mart generation literals diverge from pipeline.mart_config.DEFAULT_MART_DB_NAME "
        f"({DEFAULT_MART_DB_NAME}):\n" + "\n".join(mismatches)
    )


def test_python_literal_only_in_declared_files() -> None:
    offenders: list[str] = []
    for path in _scan_files():
        if path.suffix != ".py":
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ALLOWED_PY_LITERAL_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if DEFAULT_MART_DB_NAME in text:
            offenders.append(rel)
    assert not offenders, (
        "New hardcoded mart DB literals found; import pipeline.mart_config instead "
        "(or add a standalone script to ALLOWED_PY_LITERAL_FILES with justification):\n"
        + "\n".join(offenders)
    )


def test_source_epoch_is_generation_name() -> None:
    assert DEFAULT_SOURCE_EPOCH == DEFAULT_MART_DB_NAME


def test_resolver_prefers_env_then_default(monkeypatch) -> None:
    monkeypatch.delenv("X_MART_A", raising=False)
    monkeypatch.setenv("X_MART_B", "other_db")

    assert resolve_mart_db_name("X_MART_A", "X_MART_B") == "other_db"
    assert resolve_mart_db_name("X_MART_A") == DEFAULT_MART_DB_NAME
