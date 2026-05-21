#!/usr/bin/env python3
"""Verify Layer 1 IQVIA NSA raw MariaDB load with read-only SELECTs."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

from iqvia_loader import NSA_TABLE, connect
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l1"
REQUESTED_IQVIA_SOURCE_ROOT = PROJECT_ROOT / "data" / "processed" / "2026-04-01"
ACTUAL_IQVIA_SOURCE_ROOT = PROJECT_ROOT / "data" / "IQVIA" / "NSA"
EXPECTED_TOTAL_ROWS = 2_670_000
EXPECTED_TOTAL_TOLERANCE = 100_000
JW_BRANDS = [
    "리바로",
    "리바로젯",
    "리바로브이",
    "리바로페노",
    "페린젝트",
    "시그마트",
    "가드메트",
    "타발리스",
]


def quarter_label(year: int | str, quarter: int | str) -> str:
    return f"{int(year):04d}Q{int(quarter)}"


def expected_quarters(start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    year, quarter = start
    labels: list[str] = []
    while (year, quarter) <= end:
        labels.append(quarter_label(year, quarter))
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1
    return labels


def status_for_total(total_rows: int) -> str:
    return "PASS" if abs(total_rows - EXPECTED_TOTAL_ROWS) <= EXPECTED_TOTAL_TOLERANCE else "WARN"


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def discover_external_csv() -> Path | None:
    preferred = ACTUAL_IQVIA_SOURCE_ROOT / "NSA_IQVIA_2025 4Q.csv"
    if preferred.exists():
        return preferred
    files = sorted(ACTUAL_IQVIA_SOURCE_ROOT.glob("NSA_IQVIA*.csv")) if ACTUAL_IQVIA_SOURCE_ROOT.exists() else []
    return files[0] if files else None


def source_inventory() -> dict[str, Any]:
    actual_csv = sorted(ACTUAL_IQVIA_SOURCE_ROOT.glob("NSA_IQVIA*.csv")) if ACTUAL_IQVIA_SOURCE_ROOT.exists() else []
    requested_csv = sorted(REQUESTED_IQVIA_SOURCE_ROOT.glob("NSA_IQVIA*.csv")) if REQUESTED_IQVIA_SOURCE_ROOT.exists() else []
    return {
        "requested_source_root": str(REQUESTED_IQVIA_SOURCE_ROOT.relative_to(PROJECT_ROOT)),
        "requested_source_root_exists": REQUESTED_IQVIA_SOURCE_ROOT.exists(),
        "requested_csv_count": len(requested_csv),
        "actual_source_root": str(ACTUAL_IQVIA_SOURCE_ROOT.relative_to(PROJECT_ROOT)),
        "actual_source_root_exists": ACTUAL_IQVIA_SOURCE_ROOT.exists(),
        "actual_csv_count": len(actual_csv),
        "note": "The request named data/processed/2026-04-01, but this repository's IQVIA loader uses data/IQVIA/NSA.",
    }


def fetch_dict_rows(cur: Any) -> list[dict[str, Any]]:
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def verify_iqvia() -> dict[str, Any]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    checks: list[dict[str, Any]] = []
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {NSA_TABLE}")
            total = int(cur.fetchone()[0])
            checks.append(
                {
                    "name": "total row count",
                    "value": total,
                    "expected_approx": EXPECTED_TOTAL_ROWS,
                    "tolerance": EXPECTED_TOTAL_TOLERANCE,
                    "status": status_for_total(total),
                }
            )

            cur.execute(
                f"""
                SELECT DISTINCT period_yyyy, period_quarter
                FROM {NSA_TABLE}
                ORDER BY period_yyyy, period_quarter
                """
            )
            periods = [quarter_label(year, quarter) for year, quarter in cur.fetchall()]
            expected = expected_quarters((2020, 3), (2025, 4))
            missing = [period for period in expected if period not in periods]
            extra = [period for period in periods if period not in expected]
            checks.append(
                {
                    "name": "period continuity (2020-Q3 to 2025-Q4)",
                    "value": len(periods),
                    "expected": len(expected),
                    "status": "PASS" if not missing and not extra else "FAIL",
                    "missing": missing,
                    "extra": extra,
                    "first_period": periods[0] if periods else None,
                    "last_period": periods[-1] if periods else None,
                }
            )

            cur.execute(f"SELECT payload FROM {NSA_TABLE} LIMIT 1000")
            parse_failures = 0
            static_keys: set[str] = set()
            sample_count = 0
            for (payload_str,) in cur.fetchall():
                sample_count += 1
                try:
                    payload = json.loads(payload_str)
                    if isinstance(payload, dict) and isinstance(payload.get("static"), dict):
                        static_keys.update(str(key) for key in payload["static"].keys())
                except Exception:
                    parse_failures += 1
            checks.append(
                {
                    "name": "payload JSON parse (1000 sample)",
                    "sample_count": sample_count,
                    "parse_failures": parse_failures,
                    "parse_success": sample_count - parse_failures,
                    "status": "PASS" if parse_failures == 0 else "FAIL",
                    "static_keys_observed": sorted(static_keys),
                }
            )

            cur.execute(
                f"""
                SELECT code_type, COUNT(*) AS row_count
                FROM (
                    SELECT CASE
                        WHEN LOWER(COALESCE(audit_code, '')) = 'grand total'
                          OR LOWER(COALESCE(audit_desc, '')) LIKE '%grand total%'
                        THEN 'Grand Total'
                        ELSE 'individual'
                    END AS code_type
                    FROM {NSA_TABLE}
                ) typed
                GROUP BY code_type
                ORDER BY row_count DESC
                """
            )
            audit_code_distribution = {row[0]: int(row[1]) for row in cur.fetchall()}
            checks.append(
                {
                    "name": "audit_code Grand Total vs individual",
                    "status": "INFO",
                    "distribution": audit_code_distribution,
                }
            )

            cur.execute(
                f"""
                SELECT audit_desc, COUNT(*) AS row_count
                FROM {NSA_TABLE}
                GROUP BY audit_desc
                ORDER BY row_count DESC
                LIMIT 20
                """
            )
            audit_desc_distribution = {str(row[0]): int(row[1]) for row in cur.fetchall()}
            checks.append(
                {
                    "name": "audit_desc (channel) distribution",
                    "status": "INFO",
                    "distribution": audit_desc_distribution,
                }
            )

            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_rows,
                    SUM(
                        CASE
                            WHEN JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static."ATC 4 CODE"')) IS NULL
                              OR JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static."ATC 4 CODE"')) = ''
                            THEN 1 ELSE 0
                        END
                    ) AS null_count
                FROM {NSA_TABLE}
                WHERE NOT (
                    LOWER(COALESCE(audit_code, '')) = 'grand total'
                    OR LOWER(COALESCE(audit_desc, '')) LIKE '%grand total%'
                )
                """
            )
            atc_total, atc_null = cur.fetchone()
            atc_total = int(atc_total or 0)
            atc_null = int(atc_null or 0)
            checks.append(
                {
                    "name": "ATC 4 CODE null rate (non-Grand Total)",
                    "status": "PASS" if atc_total and atc_null == 0 else "WARN",
                    "total": atc_total,
                    "null_count": atc_null,
                    "null_pct": round(atc_null / atc_total * 100, 4) if atc_total else 0,
                }
            )

            cur.execute(
                f"""
                SELECT source_file, COUNT(*) AS row_count
                FROM {NSA_TABLE}
                GROUP BY source_file
                ORDER BY row_count DESC
                """
            )
            source_file_counts = {str(row[0]): int(row[1]) for row in cur.fetchall()}

            cur.execute(
                f"""
                SELECT period_yyyy, period_quarter, COUNT(*) AS row_count
                FROM {NSA_TABLE}
                GROUP BY period_yyyy, period_quarter
                ORDER BY period_yyyy, period_quarter
                """
            )
            period_distribution = [
                {"period": quarter_label(year, quarter), "rows": int(rows)}
                for year, quarter, rows in cur.fetchall()
            ]

            external_csv = discover_external_csv()
            external_cross_check = None
            if external_csv:
                external_rows = count_csv_rows(external_csv)
                cur.execute(f"SELECT COUNT(*) FROM {NSA_TABLE} WHERE source_file = %s", (external_csv.name,))
                layer1_rows = int(cur.fetchone()[0])
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT CONCAT(period_yyyy, 'Q', period_quarter))
                    FROM {NSA_TABLE}
                    WHERE source_file = %s
                    """,
                    (external_csv.name,),
                )
                source_period_count = int(cur.fetchone()[0])
                expected_upper = external_rows * max(source_period_count, 1)
                external_cross_check = {
                    "name": f"external cross-check: {external_csv.name}",
                    "status": "PASS" if layer1_rows > 0 and layer1_rows <= expected_upper else "WARN",
                    "external_rows": external_rows,
                    "layer1_rows": layer1_rows,
                    "source_period_count": source_period_count,
                    "expected_upper_rows": expected_upper,
                    "layer1_to_external_ratio": round(layer1_rows / external_rows, 4) if external_rows else None,
                    "source_path": str(external_csv.relative_to(PROJECT_ROOT)),
                    "note": "NSA source has wide period value columns; Layer 1 keeps one row per populated source row and quarter.",
                }
                checks.append(external_cross_check)
            else:
                checks.append(
                    {
                        "name": "external cross-check: IQVIA CSV discovery",
                        "status": "WARN",
                        "note": "No NSA_IQVIA*.csv source file found under data/IQVIA/NSA.",
                    }
                )

            jw_selects = [
                f"SUM(CASE WHEN product_name LIKE %s THEN 1 ELSE 0 END) AS brand_{idx}"
                for idx, _brand in enumerate(JW_BRANDS)
            ]
            cur.execute(
                f"""
                SELECT {", ".join(jw_selects)}
                FROM (
                    SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static."PRODUCT NAME KOR"')) AS product_name
                    FROM {NSA_TABLE}
                ) products
                """,
                tuple(f"%{brand}%" for brand in JW_BRANDS),
            )
            jw_row = cur.fetchone()
            jw_counts = {brand: int(jw_row[idx] or 0) for idx, brand in enumerate(JW_BRANDS)}
            checks.append(
                {
                    "name": "JW brand row counts",
                    "status": "PASS" if any(count > 0 for count in jw_counts.values()) else "WARN",
                    "brands_checked": len(jw_counts),
                }
            )
    finally:
        conn.close()

    return {
        "phase": "16-G-4-Side-Verify-L1",
        "layer": "L1 IQVIA NSA raw",
        "generated_at": generated_at,
        "source_inventory": source_inventory(),
        "checks": checks,
        "source_file_counts": source_file_counts,
        "period_distribution": period_distribution,
        "audit_code_distribution": audit_code_distribution,
        "audit_desc_distribution": audit_desc_distribution,
        "jw_brand_row_counts": jw_counts,
        "external_cross_check": external_cross_check,
        "notes": [
            "페린젝트는 IQVIA NSA에서 row > 0이 예상되며 UBIST 0 row와 대비되는 known issue이다.",
            "audit_code/audit_desc Grand Total row는 individual channel row와 중복 집계될 수 있어 L2/L3에서 별도 처리해야 한다.",
            "본 검증은 MariaDB SELECT와 원본 CSV read만 수행한다.",
        ],
    }


def write_result(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIT_DIR / "02_iqvia_verification.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    result = verify_iqvia()
    out_path = write_result(result)
    total_check = next(check for check in result["checks"] if check["name"] == "total row count")
    period_check = next(check for check in result["checks"] if check["name"].startswith("period continuity"))
    print(f"IQVIA rows: {total_check['value']:,} ({total_check['status']})")
    print(f"IQVIA periods: {period_check['value']} / {period_check['expected']} ({period_check['status']})")
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
