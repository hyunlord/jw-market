#!/usr/bin/env python3
"""Phase 30.3 validation for separated ai_analysis ownership."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymysql

try:
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25


BASE_URL = "http://127.0.0.1:8013"
TARGET_TABLE = "cache_deep_analysis_ai_analysis"
REQUIRED_SECTIONS = ("phenomenon", "cause", "prediction", "recommendation")
JW25 = sorted(CANONICAL_25)


@dataclass
class Issue:
    kind: str
    brand: str | None = None
    detail: dict[str, Any] | None = None


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _api_payload(brand: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(brand)
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/deep-analysis/{encoded}", timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:  # pragma: no cover - operational gate
        return {"_error": str(exc)}
    return payload


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES LIKE %s", (TARGET_TABLE,))
        if not cur.fetchone():
            issues.append(Issue("ai_analysis_table_missing"))
            return _report(issues, 0, 0)

        placeholders = ",".join(["%s"] * len(JW25))
        cur.execute(
            f"""
            SELECT brand, market_id, ai_analysis_json,
                   JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.phase_zeta_stage')) AS stage,
                   updated_at
            FROM {TARGET_TABLE}
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            JW25,
        )
        ai_rows = {str(row["brand"]): row for row in cur.fetchall()}

        for brand in JW25:
            row = ai_rows.get(brand)
            if not row:
                issues.append(Issue("ai_analysis_row_missing", brand))
                continue
            raw = row.get("ai_analysis_json")
            try:
                ai = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                issues.append(Issue("ai_analysis_invalid_json", brand, {"error": str(exc)}))
                continue
            if not ai.get("phase_zeta_stage"):
                issues.append(Issue("phase_zeta_stage_missing", brand))
            for section in REQUIRED_SECTIONS:
                section_payload = ai.get(section)
                if not isinstance(section_payload, dict) or not section_payload:
                    issues.append(Issue("ai_analysis_section_missing", brand, {"section": section}))

        cur.execute(
            f"""
            SELECT brand,
                   JSON_CONTAINS_PATH(response_json, 'one', '$.data.ai_analysis') AS has_ai_analysis
            FROM cache_deep_analysis
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            JW25,
        )
        cache_rows = cur.fetchall()
    finally:
        conn.close()

    for row in cache_rows:
        if int(row.get("has_ai_analysis") or 0) != 0:
            issues.append(Issue("cache_deep_analysis_still_stores_ai_analysis", row["brand"]))

    api_checked = 0
    for brand in JW25:
        payload = _api_payload(brand)
        if not payload or payload.get("_error"):
            issues.append(Issue("backend_api_error", brand, {"error": None if not payload else payload.get("_error")}))
            continue
        api_checked += 1
        ai = ((payload.get("data") or {}).get("ai_analysis") or {})
        if not ai.get("phase_zeta_stage"):
            issues.append(Issue("backend_api_ai_analysis_marker_missing", brand))
        for section in REQUIRED_SECTIONS:
            if not ai.get(section):
                issues.append(Issue("backend_api_ai_analysis_section_missing", brand, {"section": section}))

    return _report(issues, len(ai_rows), api_checked)


def _report(issues: list[Issue], table_rows: int, api_checked: int) -> dict[str, Any]:
    return {
        "phase": "30.3",
        "validator": "ai_analysis_separation",
        "jw_brands": len(JW25),
        "table_rows_checked": table_rows,
        "api_payloads_checked": api_checked,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
