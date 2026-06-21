# /// script
# requires-python = ">=3.11"
# dependencies = ["pymysql", "numpy", "scikit-learn"]
# ///
# --- How to run ---
# python3 -m pipeline.scripts.analysis.brand_activity.topic_redesign.run_topic_redesign
"""Generate the read-only topic label redesign reports and audit package."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path

from .audit import AUDIT_ROOT, DOCS_DIR, backup_zip, create_zip_package, run_scans, run_summary, write_audit_artifacts, write_findings, write_manifest
from .db_io import connect_mariadb, count_by_market, fetch_db_payload, group_by_market, read_env_file, validate_market_scope
from .dictionary import assign_labels, build_label_candidates, coverage_by_market
from .methods import score_extraction_methods
from .models import CoverageRow, JsonValue, LabelCandidate, MessageRow, MethodScore
from .reports import (
    dictionary_json,
    render_coverage_test,
    render_dictionary_draft,
    render_eval_handoff,
    render_label_candidates,
    render_method_comparison,
    write_json,
)
from .text import token_counts


FIRST_POC_BASELINE = {"A02B2": 0.449, "C10C0": 0.311, "A10N1": 0.290, "G04C2": 0.213, "C10A1": 0.443}


def main() -> int:
    """Run the full read-only analysis, report generation, verification, and packaging."""
    timestamp = datetime.now().astimezone()
    tag = timestamp.strftime("%Y%m%d_%H%M%S")
    generated_at = timestamp.isoformat(timespec="seconds")
    audit_dir = AUDIT_ROOT / tag
    audit_dir.mkdir(parents=True, exist_ok=True)
    env = read_env_file()
    connection = connect_mariadb(env)
    try:
        db_payload = fetch_db_payload(connection)
    finally:
        connection.close()
    keyword_rows = checked_rows(db_payload, "keyword_rows")
    auxiliary_rows = checked_rows(db_payload, "auxiliary_rows")
    market_counts = count_by_market(keyword_rows)
    markets = tuple(market_counts)
    validate_market_scope(markets)
    method_scores = score_extraction_methods(keyword_rows, sample_markets(market_counts))
    candidates = build_all_candidates(keyword_rows, auxiliary_rows, markets)
    assignments = assign_labels(keyword_rows, candidates)
    coverage = coverage_by_market(keyword_rows, assignments)
    residual = residual_terms(keyword_rows, assignments)
    dictionary_path = DOCS_DIR / "REDESIGN_03_DICTIONARY_DRAFT.json"
    write_reports(generated_at, method_scores, candidates, market_counts, coverage, residual, dictionary_path)
    write_audit_artifacts(audit_dir, db_payload, method_scores, candidates, coverage, residual, assignments)
    scan_payload = run_scans(audit_dir, env, keyword_rows)
    write_json(audit_dir / "privacy_secret_scan.json", scan_payload)
    write_findings(audit_dir, generated_at, candidates, coverage, method_scores, scan_payload)
    manifest_rows = write_manifest(audit_dir)
    zip_path, zip_sha = create_zip_package(tag, audit_dir)
    backup_path = backup_zip(zip_path, zip_sha, tag)
    summary = run_summary(tag, generated_at, candidates, coverage, method_scores, scan_payload, manifest_rows, zip_path, zip_sha, backup_path)
    write_json(audit_dir / "run_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def checked_rows(db_payload: dict[str, JsonValue | list[MessageRow]], key: str) -> list[MessageRow]:
    """Return typed rows from the mixed DB payload."""
    value = db_payload[key]
    if not isinstance(value, list):
        raise TypeError(f"expected list payload for {key}")
    return value


def sample_markets(market_counts: dict[str, int]) -> tuple[str, ...]:
    """Return the requested method-comparison sample markets that exist."""
    return tuple(market for market in ("A02B2", "C10C0", "G04C2", "A10N1", "A07E9") if market in market_counts)


def build_all_candidates(keyword_rows: list[MessageRow], auxiliary_rows: list[MessageRow], markets: tuple[str, ...]) -> list[LabelCandidate]:
    """Build candidate dictionaries for every discovered market."""
    keyword_by_market = group_by_market(keyword_rows)
    auxiliary_by_market = group_by_market(auxiliary_rows)
    candidates: list[LabelCandidate] = []
    for market in markets:
        candidates.extend(build_label_candidates(market, keyword_by_market.get(market, []), auxiliary_by_market.get(market, [])))
    return candidates


def residual_terms(rows: list[MessageRow], assignments: dict[str, tuple[str, ...]]) -> dict[str, list[tuple[str, int]]]:
    """Summarize residual unmatched token signals without storing raw text."""
    residual_by_market: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        if not assignments.get(row.row_id):
            residual_by_market[row.market].append(row.text)
    return {market: token_counts(texts).most_common(15) for market, texts in sorted(residual_by_market.items())}


def write_reports(
    generated_at: str,
    method_scores: list[MethodScore],
    candidates: list[LabelCandidate],
    market_counts: dict[str, int],
    coverage: list[CoverageRow],
    residual: dict[str, list[tuple[str, int]]],
    dictionary_path: Path,
) -> None:
    """Write the five requested redesign reports and dictionary JSON."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(dictionary_path, dictionary_json(candidates))
    (DOCS_DIR / "REDESIGN_01_METHOD_COMPARISON.md").write_text(render_method_comparison(generated_at, method_scores), encoding="utf-8")
    (DOCS_DIR / "REDESIGN_02_LABEL_CANDIDATES.md").write_text(render_label_candidates(generated_at, candidates, market_counts), encoding="utf-8")
    (DOCS_DIR / "REDESIGN_03_DICTIONARY_DRAFT.md").write_text(render_dictionary_draft(generated_at, candidates, dictionary_path.name), encoding="utf-8")
    (DOCS_DIR / "REDESIGN_04_COVERAGE_TEST.md").write_text(render_coverage_test(generated_at, coverage, residual, FIRST_POC_BASELINE), encoding="utf-8")
    small_markets = tuple(row.market for row in coverage if row.rows < 80)
    (DOCS_DIR / "REDESIGN_05_EVAL_AND_HANDOFF.md").write_text(render_eval_handoff(generated_at, coverage, len(candidates), small_markets), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
