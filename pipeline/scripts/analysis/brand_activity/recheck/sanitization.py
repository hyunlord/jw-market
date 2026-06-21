from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from pipeline.scripts.analysis.brand_activity.recheck.summaries import read_json, write_json


JsonObject = dict[str, Any]
SENSITIVE_TEXT_HASH_COLUMNS = {
    "keyword_text_sha256",
    "what_other_materials_sha256",
    "other_comments_sha256",
    "meeting_topic_sha256",
    "verbatim_message_sha256",
}
EXPECTED_HASH_COLUMNS_BY_FILE = {
    "keyword_events_stage_redacted.csv": [
        "keyword_text_sha256",
        "other_comments_sha256",
        "what_other_materials_sha256",
    ],
    "meeting_events_stage_redacted.csv": [
        "meeting_topic_sha256",
        "other_comments_sha256",
        "verbatim_message_sha256",
    ],
}


def sanitize_km_validation(path: Path, stage_schema: str) -> JsonObject:
    """Keep packaged KM DB inventory evidence scoped to the isolated stage schema."""
    validation = read_json(path)
    db_load = validation.get("db_load")
    if isinstance(db_load, dict):
        for key in ("inventory_before", "inventory_after"):
            rows = db_load.get(key)
            if isinstance(rows, list):
                db_load[key] = [row for row in rows if row.get("schema") == stage_schema]
    write_json(path, validation)
    return validation


def drop_sensitive_text_hash_columns(path: Path) -> JsonObject:
    """Remove dictionary-attackable free-text hashes from shareable CSV outputs."""
    if not path.exists():
        return {"removed_now": [], "package_excluded": [], "remaining_sensitive_hash_columns": []}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    dropped = [field for field in fieldnames if field in SENSITIVE_TEXT_HASH_COLUMNS]
    kept = [field for field in fieldnames if field not in SENSITIVE_TEXT_HASH_COLUMNS]
    if dropped:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=kept)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row[field] for field in kept})
    expected = EXPECTED_HASH_COLUMNS_BY_FILE.get(path.name, sorted(SENSITIVE_TEXT_HASH_COLUMNS))
    return {
        "removed_now": dropped,
        "package_excluded": expected,
        "remaining_sensitive_hash_columns": sorted(set(kept) & set(expected)),
    }


def sanitize_shareable_outputs(audit_dir: Path, output_dir: Path, stage_schema: str) -> JsonObject:
    """Apply package-facing privacy and scope reductions after legacy loaders run."""
    csv_sanitization = {
        "keyword_events_stage_redacted.csv": drop_sensitive_text_hash_columns(output_dir / "km" / "keyword_events_stage_redacted.csv"),
        "meeting_events_stage_redacted.csv": drop_sensitive_text_hash_columns(output_dir / "km" / "meeting_events_stage_redacted.csv"),
    }
    km_validation = sanitize_km_validation(audit_dir / "load_km" / "km_ingest_validation.json", stage_schema)
    report = {
        "stage_schema": stage_schema,
        "csv_sanitization": csv_sanitization,
        "km_inventory_entries_after_filter": {
            "before": len(km_validation["db_load"].get("inventory_before", [])),
            "after": len(km_validation["db_load"].get("inventory_after", [])),
        },
    }
    write_json(audit_dir / "shareable_sanitization.json", report)
    return report
