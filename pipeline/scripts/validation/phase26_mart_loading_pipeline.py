#!/usr/bin/env python3
"""Phase 26 read-only validation for mart metric_history rank/MS.

The strategic ML/CD mart rows are expected to describe their own strategic
market. This validator recomputes rank and market share from sibling brand
rows for each (market, source, measure, period) group and compares them with
the stored metric_history values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import pymysql


TABLE_SPECS = {
    "mart_general_brand_metric": {
        "market_col": "atc4_code",
        "description": "general ATC4 view",
    },
    "mart_strategic_ml_brand_metric": {
        "market_col": "ml_id",
        "description": "strategic market_landscape view",
    },
    "mart_strategic_cd_brand_metric": {
        "market_col": "cd_market_id",
        "description": "strategic competitive_dynamics view",
    },
}

MS_TOLERANCE_PCT = 0.5


@dataclass
class ValidationIssue:
    table: str
    market_id: str
    source: str
    measure: str
    brand_name: str
    period: str
    kind: str
    stored: float | int | None
    expected: float | int | None
    raw_value: float | None
    detail: dict[str, Any] = field(default_factory=dict)


def decode_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if not value.strip():
            return {}
        return json.loads(value)
    raise TypeError(f"Unsupported JSON value type: {type(value)!r}")


def period_sort_key(period: str) -> tuple[int, int, str]:
    """Sort YYYY-MM and YYYY-Qn period keys chronologically."""
    text = str(period)
    if "-Q" in text:
        year, quarter = text.split("-Q", 1)
        return (int(year), int(quarter) * 3, text)
    if "-" in text:
        year, month = text.split("-", 1)
        return (int(year), int(month), text)
    return (0, 0, text)


def latest_period(metric_history: dict[str, Any]) -> str | None:
    if not metric_history:
        return None
    return sorted(metric_history.keys(), key=period_sort_key)[-1]


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def rank_ranges_by_raw(raw_by_brand: dict[str, float]) -> dict[str, tuple[int, int]]:
    """Return tie-aware valid rank ranges by brand.

    If two brands have the same raw value, any ordinal position occupied by the
    tied group is accepted. This avoids false positives from arbitrary tie
    ordering in upstream marts.
    """
    sorted_items = sorted(raw_by_brand.items(), key=lambda item: (-item[1], item[0]))
    ranges: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(sorted_items):
        raw_value = sorted_items[index][1]
        end = index
        while end + 1 < len(sorted_items) and sorted_items[end + 1][1] == raw_value:
            end += 1
        rank_min = index + 1
        rank_max = end + 1
        for brand_name, _ in sorted_items[index : end + 1]:
            ranges[brand_name] = (rank_min, rank_max)
        index = end + 1
    return ranges


def evaluate_rows_for_period(
    *,
    table: str,
    market_id: str,
    source: str,
    measure: str,
    rows: list[dict[str, Any]],
    period: str,
) -> tuple[list[ValidationIssue], int]:
    raw_by_brand: dict[str, float] = {}
    period_payload_by_brand: dict[str, dict[str, Any]] = {}

    for row in rows:
        brand_name = str(row["brand_name"])
        metric_history = decode_json(row.get("metric_history"))
        period_payload = metric_history.get(period)
        if not isinstance(period_payload, dict):
            continue
        raw_value = safe_float(period_payload.get("raw_value"))
        if raw_value is None:
            continue
        raw_by_brand[brand_name] = raw_value
        period_payload_by_brand[brand_name] = period_payload

    if not raw_by_brand:
        return [], 0

    market_total = sum(raw_by_brand.values())
    rank_ranges = rank_ranges_by_raw(raw_by_brand)
    issues: list[ValidationIssue] = []

    for brand_name, period_payload in period_payload_by_brand.items():
        raw_value = raw_by_brand[brand_name]

        stored_rank = safe_int(period_payload.get("rank"))
        if stored_rank is not None:
            rank_min, rank_max = rank_ranges[brand_name]
            if not (rank_min <= stored_rank <= rank_max):
                issues.append(
                    ValidationIssue(
                        table=table,
                        market_id=market_id,
                        source=source,
                        measure=measure,
                        brand_name=brand_name,
                        period=period,
                        kind="rank",
                        stored=stored_rank,
                        expected=rank_min if rank_min == rank_max else None,
                        raw_value=raw_value,
                        detail={"expected_rank_min": rank_min, "expected_rank_max": rank_max},
                    )
                )

        stored_ms = safe_float(period_payload.get("ms"))
        if stored_ms is not None:
            expected_ms = (raw_value / market_total * 100.0) if market_total > 0 else 0.0
            if abs(stored_ms - expected_ms) > MS_TOLERANCE_PCT:
                issues.append(
                    ValidationIssue(
                        table=table,
                        market_id=market_id,
                        source=source,
                        measure=measure,
                        brand_name=brand_name,
                        period=period,
                        kind="ms",
                        stored=stored_ms,
                        expected=expected_ms,
                        raw_value=raw_value,
                        detail={"market_total": market_total, "tolerance_pct": MS_TOLERANCE_PCT},
                    )
                )

    return issues, len(period_payload_by_brand)


def connect_db() -> pymysql.connections.Connection:
    user = os.environ.get("DB_USER", "root")
    password = os.environ.get("DB_ROOT_PASSWORD") or os.environ.get("DB_PASSWORD") or ""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3308")),
        user=user,
        password=password,
        database=os.environ.get("DB_NAME", "jw_mart"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_combinations(conn: pymysql.connections.Connection, table: str, market_col: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT {market_col} AS market_id, source, measure
            FROM {table}
            ORDER BY {market_col}, source, measure
            """
        )
        return list(cur.fetchall())


def fetch_rows(
    conn: pymysql.connections.Connection,
    table: str,
    market_col: str,
    market_id: str,
    source: str,
    measure: str,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_name, metric_history
            FROM {table}
            WHERE {market_col} = %s
              AND source = %s
              AND measure = %s
            ORDER BY brand_name
            """,
            (market_id, source, measure),
        )
        return list(cur.fetchall())


def validate_table(
    conn: pymysql.connections.Connection,
    table: str,
    *,
    latest_only: bool,
    max_issues: int | None = None,
) -> dict[str, Any]:
    spec = TABLE_SPECS[table]
    market_col = spec["market_col"]
    combinations = fetch_combinations(conn, table, market_col)
    issues: list[ValidationIssue] = []
    issues_count_total = 0
    issue_kind_counts: Counter[str] = Counter()
    issue_market_counts: Counter[str] = Counter()
    issue_brand_counts: Counter[str] = Counter()
    checked_period_rows = 0
    checked_groups = 0
    periods_seen: Counter[str] = Counter()

    for combo in combinations:
        market_id = str(combo["market_id"])
        source = str(combo["source"])
        measure = str(combo["measure"])
        rows = fetch_rows(conn, table, market_col, market_id, source, measure)
        if not rows:
            continue

        periods: set[str] = set()
        for row in rows:
            history = decode_json(row.get("metric_history"))
            if latest_only:
                period = latest_period(history)
                if period:
                    periods.add(period)
            else:
                periods.update(str(period) for period in history.keys())

        for period in sorted(periods, key=period_sort_key):
            group_issues, row_count = evaluate_rows_for_period(
                table=table,
                market_id=market_id,
                source=source,
                measure=measure,
                rows=rows,
                period=period,
            )
            checked_groups += 1
            checked_period_rows += row_count
            periods_seen[period] += row_count
            if group_issues:
                issues_count_total += len(group_issues)
                issue_kind_counts.update(issue.kind for issue in group_issues)
                issue_market_counts.update(issue.market_id for issue in group_issues)
                issue_brand_counts.update(issue.brand_name for issue in group_issues)
                if max_issues is None:
                    issues.extend(group_issues)
                else:
                    remaining = max_issues - len(issues)
                    if remaining > 0:
                        issues.extend(group_issues[:remaining])

    issue_dicts = [asdict(issue) for issue in issues]

    return {
        "table": table,
        "description": spec["description"],
        "latest_only": latest_only,
        "combinations": len(combinations),
        "checked_groups": checked_groups,
        "checked_period_rows": checked_period_rows,
        "issues_count": issues_count_total,
        "issues_truncated": max_issues is not None and issues_count_total > len(issues),
        "issue_kind_counts": dict(issue_kind_counts),
        "issue_market_top10": issue_market_counts.most_common(10),
        "issue_brand_top10": issue_brand_counts.most_common(10),
        "periods_seen_top10": periods_seen.most_common(10),
        "issues": issue_dicts,
    }


def run(
    *,
    tables: list[str],
    latest_only: bool,
    max_issues_per_table: int | None,
) -> dict[str, Any]:
    conn = connect_db()
    try:
        table_results = [
            validate_table(conn, table, latest_only=latest_only, max_issues=max_issues_per_table)
            for table in tables
        ]
    finally:
        conn.close()

    total_issues = sum(result["issues_count"] for result in table_results)
    total_checked = sum(result["checked_period_rows"] for result in table_results)
    return {
        "phase": "26",
        "validator": "mart_loading_rank_ms",
        "latest_only": latest_only,
        "ms_tolerance_pct": MS_TOLERANCE_PCT,
        "tables": table_results,
        "summary": {
            "tables_checked": len(table_results),
            "checked_period_rows": total_checked,
            "issues_count": total_issues,
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table",
        action="append",
        choices=sorted(TABLE_SPECS),
        help="Table to validate. May be repeated. Defaults to all mart brand tables.",
    )
    parser.add_argument("--all-periods", action="store_true", help="Validate every metric_history period, not just latest.")
    parser.add_argument("--json-out", help="Write full validation result JSON to this path.")
    parser.add_argument("--max-issues-per-table", type=int, help="Truncate stored issue examples per table.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run(
        tables=args.table or list(TABLE_SPECS),
        latest_only=not args.all_periods,
        max_issues_per_table=args.max_issues_per_table,
    )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)

    print("=== Phase 26 Mart Loading Validation ===")
    print(f"latest_only={result['latest_only']} ms_tolerance_pct={result['ms_tolerance_pct']}")
    print(
        "summary="
        + json.dumps(
            result["summary"],
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    for table_result in result["tables"]:
        print(
            f"{table_result['table']}: checked={table_result['checked_period_rows']} "
            f"issues={table_result['issues_count']} kinds={table_result['issue_kind_counts']}"
        )
        for issue in table_result["issues"][:10]:
            print(json.dumps(issue, ensure_ascii=False, sort_keys=True))

    return 1 if result["summary"]["issues_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
