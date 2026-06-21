#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymysql"]
# ///
"""Generate Brand Activity alias mapping artifacts.

Usage:
  python3 pipeline/scripts/analysis/brand_activity/alias/run_alias_mapping.py \
    --output-dir docs/design/brand_activity/alias
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from pipeline.etl.io.catalog.postfix.canonical import CANONICAL_BRANDS  # noqa: E402
from pipeline.scripts.analysis.brand_activity.alias.bridge import fetch_bridge_molecules  # noqa: E402
from pipeline.scripts.analysis.brand_activity.alias.builder import (  # noqa: E402
    KorEvidence,
    build_alias_records,
)
from pipeline.scripts.analysis.brand_activity.alias.io_sources import (  # noqa: E402
    connect_stage_db,
    fetch_stage_observations,
    fetch_stage_snapshot,
    load_nsa_evidence,
    sha256_file,
)
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en  # noqa: E402
from pipeline.scripts.analysis.brand_activity.alias.reports import (  # noqa: E402
    render_mapping_json,
    render_review_md,
    render_validation_md,
    write_json,
)


def compact_text(value: str) -> str:
    return "".join(value.split()).upper()


def canonical_alias_map() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for spec in CANONICAL_BRANDS:
        candidates = [spec.name, spec.source_key or "", *spec.contains]
        for candidate in candidates:
            if candidate:
                aliases[compact_text(candidate)] = spec.name
    return aliases


def normalize_kor_evidence(
    kor_evidence: dict[str, KorEvidence],
    aliases: dict[str, str],
) -> dict[str, KorEvidence]:
    normalized: dict[str, KorEvidence] = {}
    ordered_aliases = sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
    for anchor, evidence in kor_evidence.items():
        compact = compact_text(evidence.kr_name)
        canonical = aliases.get(compact)
        if canonical is None:
            for alias_key, alias_value in ordered_aliases:
                if alias_key and alias_key in compact:
                    canonical = alias_value
                    break
        if canonical is None:
            normalized[anchor] = evidence
        else:
            normalized[anchor] = KorEvidence(canonical, evidence.evidence_type, evidence.evidence_source)
    return normalized


def merge_molecule_sources(
    nsa_molecules: dict[str, tuple[str, ...]],
    bridge_molecules: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for anchor in sorted(set(nsa_molecules) | set(bridge_molecules)):
        merged[anchor] = tuple(sorted({*nsa_molecules.get(anchor, ()), *bridge_molecules.get(anchor, ())}))
    return merged


def unresolved_questions(
    result_count_pending: int,
    missing_jw: list[str],
    snapshot: dict[str, object],
    nsa_confidence_gap: bool,
) -> list[str]:
    questions = [
        "CSD stage has no `manufacturer` column although original CSD workbooks had Manufacturer; should stage schema retain it later?",
        "CSD-only product ATC4 is inferred only for known CSD market names; PL should confirm full CSD market to ATC4 bridge.",
        "Meeting stage has `pharma_sponsor` but no representing-company field; PL should decide whether sponsor belongs in alias company metadata.",
    ]
    if result_count_pending:
        questions.append(f"{result_count_pending} anchors have no Korean/canonical evidence and remain `pending`.")
    if missing_jw:
        questions.append(f"JW canonical names not mapped to an English anchor: {', '.join(missing_jw)}")
    missing_products = snapshot.get("keyword_missing_csd_products", [])
    if missing_products:
        questions.append("Keyword products without exact CSD match include CSD-uncovered and CSD-covered cases; PL should review flagged rows.")
    if nsa_confidence_gap:
        questions.append("Some stage anchors were not found in local NSA CSV `PRODUCT NAME`; keep `pending`/fallback status until source is confirmed.")
    return questions


def copy_audit_sources(repo_root: Path, audit_dir: Path) -> None:
    scripts_dir = audit_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/normalize.py",
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/bridge.py",
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/builder.py",
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/io_sources.py",
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/reports.py",
        repo_root / "pipeline/scripts/analysis/brand_activity/alias/run_alias_mapping.py",
        repo_root / "tests/analysis/brand_activity/test_alias_mapping.py",
    ]
    for path in paths:
        shutil.copy2(path, scripts_dir / path.name)


def write_manifest(audit_dir: Path, generated_files: list[Path]) -> None:
    audit_files = [path for path in audit_dir.rglob("*") if path.is_file() and path.name != "manifest.json"]
    all_files = sorted(set(generated_files + audit_files))
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "constraints": [
            "no operational DB/cache/mart writes",
            "read jw_brand_activity_stage only",
            "only A-PITO/APITO and LOWOSMOPERI/LOW OSMO PERI variant rules",
            "similar or suffix products are not auto-merged",
        ],
        "files": [
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in all_files
            if path.exists() and path.is_file()
        ],
    }
    (audit_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("docs/design/brand_activity/alias"))
    parser.add_argument("--nsa-dir", type=Path, default=Path("data/IQVIA/NSA"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[5]
    output_dir = (repo_root / args.output_dir).resolve()
    audit_dir = output_dir / "audit"
    snapshots_dir = audit_dir / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_stage_db(repo_root)
    try:
        observations = fetch_stage_observations(conn)
        snapshot = fetch_stage_snapshot(conn)
        normalized_anchors = {normalize_iqvia_en(observation.product_name) for observation in observations}
        nsa = load_nsa_evidence((repo_root / args.nsa_dir).resolve(), normalized_anchors)
        bridge_db, bridge_molecules = fetch_bridge_molecules(conn, normalized_anchors)
    finally:
        conn.close()

    kr_aliases = canonical_alias_map()
    kor_evidence = normalize_kor_evidence(nsa.kor_evidence, kr_aliases)
    jw_canonicals = tuple(spec.name for spec in CANONICAL_BRANDS)
    molecules = merge_molecule_sources(nsa.molecule_by_anchor, bridge_molecules)
    result = build_alias_records(
        observations,
        kor_evidence,
        set(jw_canonicals),
        molecule_by_anchor=molecules,
        extra_atc4_by_anchor=nsa.atc4_by_anchor,
        manufacturer_by_anchor=nsa.manufacturer_by_anchor,
    )
    mapped_jw = sorted({record.kr_canonical for record in result.records if record.kr_canonical})
    missing_jw = [name for name in jw_canonicals if name not in mapped_jw]
    questions = unresolved_questions(
        result.stats.status_distribution.get("pending", 0),
        missing_jw,
        snapshot,
        nsa.summary["anchors_with_kor"] != result.stats.anchor_count,
    )

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor": "IQVIA English PRODUCT NAME",
        "stage_schema": "jw_brand_activity_stage",
        "nsa_evidence": nsa.summary,
        "molecule_bridge_db": bridge_db,
        "jw_canonical_source": "pipeline/etl/io/catalog/postfix/canonical.py",
    }
    mapping_path = output_dir / "ALIAS_01_MAPPING.json"
    review_path = output_dir / "ALIAS_02_REVIEW.md"
    validation_path = output_dir / "ALIAS_03_VALIDATION.md"
    write_json(mapping_path, render_mapping_json(result, snapshot, metadata))
    review_path.write_text(render_review_md(result, jw_canonicals, questions))
    validation_path.write_text(render_validation_md(result, snapshot, jw_canonicals, questions))
    write_json(snapshots_dir / "input_stage_snapshot.json", snapshot)
    write_json(snapshots_dir / "nsa_evidence_summary.json", nsa.summary)
    write_json(snapshots_dir / "alias_build_stats.json", asdict(result.stats))
    copy_audit_sources(repo_root, audit_dir)
    write_manifest(
        audit_dir,
        [
            mapping_path,
            review_path,
            validation_path,
            snapshots_dir / "input_stage_snapshot.json",
            snapshots_dir / "nsa_evidence_summary.json",
            snapshots_dir / "alias_build_stats.json",
        ],
    )
    print(json.dumps({
        "anchor_count": result.stats.anchor_count,
        "configured_variant_rules": result.stats.configured_variant_rule_count,
        "observed_multi_variant_rules": result.stats.observed_multi_variant_rule_count,
        "jw_mapped": f"{result.stats.jw_mapped_count}/{len(jw_canonicals)}",
        "csd_uncovered": result.stats.csd_uncovered_count,
        "mapping_status": result.stats.status_distribution,
        "unresolved_questions": len(questions),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
