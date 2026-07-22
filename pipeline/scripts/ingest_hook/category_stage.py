"""Write category parser output to an isolated, deterministic staging artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from pipeline.scripts.ingest_hook.workbook_contracts import summarize


TARGET_TABLES = {
    "iqvia_nsa": "iqvia_nsa_quarterly_raw",
    "iqvia_csd_channel": "csd_channel_dynamics_stage",
    "iqvia_csd_keyword": "km_keyword_event_stage",
    "mi_master": "catalog_* (s2 master extracts)",
}

DEDUP_POLICIES = {
    "iqvia_nsa": (
        "natural=(audit_code,mfr_code,product_name,pack_desc,period_label); "
        "collapse exact payloads, preserve conflicts"
    ),
    "iqvia_csd_channel": (
        "raw identity=source_row_key; stage natural=(period_ym,market,jw_channel,"
        "master_product,representing_company); latest source wins"
    ),
    "iqvia_csd_keyword": (
        "raw identity=sha256(keyword,source_file,source_row_no); preserve duplicate source rows"
    ),
    "mi_master": "catalog extract primary keys; upsert by each catalog table primary key",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage(category: str, source: Path, target_dir: Path, epoch: str) -> dict[str, object]:
    summary = summarize(category, source, epoch)
    target_dir.mkdir(parents=True, exist_ok=True)
    source_sha = _sha256(source)
    artifact_stem = f"{source.stem}.{source_sha[:12]}"
    staged_source = target_dir / f"{artifact_stem}.staged{source.suffix.lower()}"
    shutil.copyfile(source, staged_source)
    if _sha256(staged_source) != source_sha:
        raise RuntimeError(f"staging copy hash mismatch: {source.name}")

    metadata = target_dir / f"{artifact_stem}.staged.json"
    metadata.write_text(
        json.dumps(
            {
                "category": category,
                "epoch": epoch,
                "source_file": source.name,
                "target_table": TARGET_TABLES[category],
                "dedup_policy": DEDUP_POLICIES[category],
                "rows": summary.rows,
                "periods": sorted(summary.periods),
                "parser": summary.detail,
                "source_sha256": source_sha,
                "staged_source": staged_source.name,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest_path = target_dir / "_manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"partitions": []}
    existing["schema_version"] = "ingest-category-stage-v1"
    existing["target_table"] = TARGET_TABLES[category]
    existing["dedup_policy"] = DEDUP_POLICIES[category]
    existing["partitions"] = [
        item for item in existing["partitions"]
        if item.get("source_sha256") != source_sha
    ]
    existing["partitions"].append(
        {
            "period_yyyymm": epoch,
            "row_count": summary.rows,
            "path": staged_source.name,
            "metadata_path": metadata.name,
            "source_file": source.name,
            "source_sha256": source_sha,
        }
    )
    manifest_path.write_text(json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return {
        "rows": summary.rows,
        "periods": sorted(summary.periods),
        "artifact": str(staged_source),
        "metadata": str(metadata),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True, choices=sorted(TARGET_TABLES))
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--target-dir", required=True, type=Path)
    parser.add_argument("--epoch", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(stage(args.category, args.file, args.target_dir, args.epoch), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
