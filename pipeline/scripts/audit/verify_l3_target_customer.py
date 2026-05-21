#!/usr/bin/env python3
"""Verify target_customer_competition payloads in Layer 3 market marts."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from api import db
from api.utils import loads_json_maybe
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l3"
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
FIXED_IQVIA_CHANNELS = {"KHPA", "KCPA", "KPA"}


def parse_json(value: Any, default: Any) -> Any:
    parsed = loads_json_maybe(value)
    return parsed if parsed is not None else default


def clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def load_market_catalog(name: str, id_col: str) -> dict[str, dict[str, Any]]:
    frame = pd.read_parquet(CATALOG_DIR / name / f"{name}.parquet")
    return {
        str(row[id_col]): {col: clean(row[col]) for col in frame.columns}
        for _, row in frame.iterrows()
    }


def source_type_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        tcc = parse_json(row.get("target_customer_competition"), {})
        counter[str(tcc.get("source_type"))] += 1
    return dict(counter)


def fetch_market_rows(table: str, id_col: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        f"""
        SELECT '{table}' AS table_name, {id_col} AS market_id, source, measure, target_customer_competition
        FROM {table}
        ORDER BY {id_col}, source, measure
        """
    )


def latest_entries(tcc: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    latest = tcc.get("latest") if isinstance(tcc, dict) else None
    if not isinstance(latest, dict):
        return "missing", []
    if isinstance(latest.get("top4"), list):
        return "top4", latest["top4"]
    if isinstance(latest.get("distributions"), list):
        return "distributions", latest["distributions"]
    return "unknown", []


def check_iqvia_fixed_channels(rows: list[dict[str, Any]]) -> dict[str, Any]:
    iqvia_rows = [row for row in rows if row.get("source") == "iqvia_nsa"]
    violations: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for row in iqvia_rows:
        tcc = parse_json(row["target_customer_competition"], {})
        entry_type, entries = latest_entries(tcc)
        channels = [entry.get("channel") for entry in entries if isinstance(entry, dict)]
        observed = set(channels)
        channel_field = set(tcc.get("channels") or [])
        ok = entry_type == "distributions" and len(entries) == 3 and observed == FIXED_IQVIA_CHANNELS
        if channel_field and channel_field != FIXED_IQVIA_CHANNELS:
            ok = False
        sample = {
            "table": row["table_name"],
            "market_id": row["market_id"],
            "measure": row["measure"],
            "source_type": tcc.get("source_type"),
            "entry_type": entry_type,
            "channels": channels,
        }
        if len(samples) < 20:
            samples.append(sample)
        if not ok:
            violations.append(sample)
    return {
        "name": "IQVIA target customer fixed KHPA/KCPA/KPA",
        "rows_checked": len(iqvia_rows),
        "violation_count": len(violations),
        "violation_samples": violations[:20],
        "samples": samples,
        "status": "PASS" if not violations else "FAIL",
    }


def check_ubist_four_entries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ubist_rows = [
        row
        for row in rows
        if row.get("source") == "ubist" and row["table_name"] != "mart_general_market_metric"
    ]
    violations: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for row in ubist_rows:
        tcc = parse_json(row["target_customer_competition"], {})
        entry_type, entries = latest_entries(tcc)
        labels = [entry.get("label") for entry in entries if isinstance(entry, dict)]
        ok = entry_type == "top4" and len(entries) == 4 and all(labels)
        sample = {
            "table": row["table_name"],
            "market_id": row["market_id"],
            "measure": row["measure"],
            "source_type": tcc.get("source_type"),
            "entry_type": entry_type,
            "top_count": len(entries),
            "labels": labels,
            "sources": [entry.get("source") for entry in entries if isinstance(entry, dict)],
        }
        if len(samples) < 20:
            samples.append(sample)
        if not ok:
            violations.append(sample)
    return {
        "name": "UBIST strategic target customer top4 has 4 raw Korean labels",
        "rows_checked": len(ubist_rows),
        "violation_count": len(violations),
        "violation_samples": violations[:20],
        "samples": samples,
        "status": "PASS" if not violations else "FAIL",
    }


def check_general_ubist_entry_availability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    general_rows = [
        row
        for row in rows
        if row.get("source") == "ubist" and row["table_name"] == "mart_general_market_metric"
    ]
    sparse: list[dict[str, Any]] = []
    count_distribution: Counter[int] = Counter()
    for row in general_rows:
        tcc = parse_json(row["target_customer_competition"], {})
        _, entries = latest_entries(tcc)
        count_distribution[len(entries)] += 1
        if len(entries) < 4:
            sparse.append(
                {
                    "market_id": row["market_id"],
                    "measure": row["measure"],
                    "source_type": tcc.get("source_type"),
                    "top_count": len(entries),
                    "labels": [entry.get("label") for entry in entries if isinstance(entry, dict)],
                }
            )
    return {
        "name": "UBIST general target customer entry availability",
        "rows_checked": len(general_rows),
        "top_count_distribution": dict(sorted(count_distribution.items())),
        "sparse_market_count": len(sparse),
        "sparse_samples": sparse[:20],
        "status": "INFO",
        "note": "General ATC4 markets can have fewer than 4 channel-specialty combinations; strategic target top4 is checked separately.",
    }


def catalog_target_count(catalog_row: dict[str, Any] | None, prefix: str) -> int:
    if not catalog_row:
        return 0
    return sum(1 for idx in range(1, 5 if prefix == "target_ubist" else 4) if catalog_row.get(f"{prefix}_{idx}"))


def expected_ubist_source_types(filled: int) -> set[str]:
    if filled == 0:
        return {"computed"}
    if filled >= 4:
        return {"catalog", "mixed"}
    return {"mixed"}


def check_ubist_catalog_priority(
    rows: list[dict[str, Any]],
    ml_catalog: dict[str, dict[str, Any]],
    cd_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source") != "ubist" or row["table_name"] == "mart_general_market_metric":
            continue
        catalog = ml_catalog if row["table_name"] == "mart_strategic_ml_market_metric" else cd_catalog
        catalog_row = catalog.get(str(row["market_id"]))
        filled = catalog_target_count(catalog_row, "target_ubist")
        tcc = parse_json(row["target_customer_competition"], {})
        _, entries = latest_entries(tcc)
        catalog_sources = sum(1 for entry in entries if isinstance(entry, dict) and entry.get("source") == "catalog")
        expected_types = expected_ubist_source_types(filled)
        ok = tcc.get("source_type") in expected_types and catalog_sources == min(filled, 4)
        sample = {
            "table": row["table_name"],
            "market_id": row["market_id"],
            "measure": row["measure"],
            "source_type": tcc.get("source_type"),
            "catalog_filled": filled,
            "catalog_sources_in_top4": catalog_sources,
            "expected_source_types": sorted(expected_types),
        }
        if len(samples) < 30:
            samples.append(sample)
        if not ok:
            violations.append(sample)
    return {
        "name": "UBIST catalog target priority source count",
        "rows_checked": len([row for row in rows if row.get("source") == "ubist" and row["table_name"] != "mart_general_market_metric"]),
        "violation_count": len(violations),
        "violation_samples": violations[:20],
        "samples": samples,
        "status": "PASS" if not violations else "WARN",
        "note": "Catalog-filled target ranks must appear first as source=catalog; empty ranks are filled by computed labels.",
    }


def check_catalog_definition_preserved(rows: list[dict[str, Any]]) -> dict[str, Any]:
    checked_catalog_entries = 0
    missing_fields: list[dict[str, Any]] = []
    detail_samples: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source") != "ubist":
            continue
        tcc = parse_json(row["target_customer_competition"], {})
        definition = tcc.get("catalog_definition")
        _, entries = latest_entries(tcc)
        catalog_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("source") == "catalog"]
        for entry in catalog_entries:
            checked_catalog_entries += 1
            missing = [key for key in ("code_label", "raw_label_candidates") if not entry.get(key)]
            if missing:
                missing_fields.append(
                    {
                        "table": row["table_name"],
                        "market_id": row["market_id"],
                        "measure": row["measure"],
                        "rank": entry.get("rank"),
                        "missing": missing,
                    }
                )
        if len(detail_samples) < 12 and (catalog_entries or definition):
            detail_samples.append(
                {
                    "table": row["table_name"],
                    "market_id": row["market_id"],
                    "measure": row["measure"],
                    "source_type": tcc.get("source_type"),
                    "catalog_definition_count": len(definition or []),
                    "top4": [
                        {
                            "rank": entry.get("rank"),
                            "source": entry.get("source"),
                            "code_label": entry.get("code_label"),
                            "label": entry.get("label"),
                            "raw_label_candidates": entry.get("raw_label_candidates"),
                        }
                        for entry in entries
                        if isinstance(entry, dict)
                    ],
                }
            )
    return {
        "name": "UBIST code_label/raw_label_candidates/catalog_definition preserved",
        "catalog_top_entries_checked": checked_catalog_entries,
        "missing_field_count": len(missing_fields),
        "missing_field_samples": missing_fields[:20],
        "detail_samples": detail_samples,
        "status": "PASS" if not missing_fields and checked_catalog_entries > 0 else "WARN",
    }


def check_general_computed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    general_rows = [row for row in rows if row["table_name"] == "mart_general_market_metric"]
    violations: list[dict[str, Any]] = []
    for row in general_rows:
        tcc = parse_json(row["target_customer_competition"], {})
        if row["source"] == "ubist" and tcc.get("source_type") != "computed":
            violations.append({"market_id": row["market_id"], "source": row["source"], "measure": row["measure"], "source_type": tcc.get("source_type")})
        if row["source"] == "iqvia_nsa" and tcc.get("source_type") != "computed_fixed":
            violations.append({"market_id": row["market_id"], "source": row["source"], "measure": row["measure"], "source_type": tcc.get("source_type")})
    return {
        "name": "general market target_customer source_type policy",
        "rows_checked": len(general_rows),
        "violation_count": len(violations),
        "violation_samples": violations[:20],
        "status": "PASS" if not violations else "WARN",
    }


def verify_target_customer() -> dict[str, Any]:
    ml_catalog = load_market_catalog("ml_market", "ml_id")
    cd_catalog = load_market_catalog("cd_market", "cd_id")
    rows = (
        fetch_market_rows("mart_general_market_metric", "atc4_code")
        + fetch_market_rows("mart_strategic_ml_market_metric", "ml_id")
        + fetch_market_rows("mart_strategic_cd_market_metric", "cd_market_id")
    )
    checks = [
        check_iqvia_fixed_channels(rows),
        check_ubist_four_entries(rows),
        check_general_ubist_entry_availability(rows),
        check_ubist_catalog_priority(rows, ml_catalog, cd_catalog),
        check_catalog_definition_preserved(rows),
        check_general_computed(rows),
    ]
    return {
        "phase": "16-G-4-Side-Verify-L3 (target_customer)",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_type_distribution": source_type_distribution(rows),
        "checks": checks,
        "policy_notes": {
            "iqvia": "IQVIA target customer must be fixed KHPA/KCPA/KPA and excludes 전체.",
            "ubist": "UBIST uses raw Korean channel/specialty labels with catalog priority and computed fill.",
        },
    }


def write_result(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / "03_target_customer_verification.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    path = write_result(verify_target_customer())
    print(f"Done: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
