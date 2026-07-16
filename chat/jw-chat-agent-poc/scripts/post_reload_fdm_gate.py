#!/usr/bin/env python3
"""Fail closed unless a promoted FDM cohort is complete and internally exact."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import re
from typing import Any, Final

try:
    from .post_reload_fdm_contract import ReloadIdentity, validate_evidence
    from .post_reload_mart_common import (
        aggregate_history_rows as _aggregate_history_rows,
        marker_for_sql as _marker_for_sql,
        normalize_utc_iso as _iso,
    )
except ImportError:
    from post_reload_fdm_contract import ReloadIdentity, validate_evidence
    from post_reload_mart_common import (
        aggregate_history_rows as _aggregate_history_rows,
        marker_for_sql as _marker_for_sql,
        normalize_utc_iso as _iso,
    )


IDENTITY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DATABASE_RE: Final = re.compile(r"^[A-Za-z0-9_]+$")
SIDECAR_DIMENSIONS: Final = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)

def _fetch_all(cursor: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def collect_runtime_evidence(identity: ReloadIdentity) -> dict[str, Any]:
    """Collect the exact FDM cohort and independent market totals read-only."""

    import pymysql

    host = os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get(
        "CHAT_CACHE_DB_HOST",
        "llmops-mariadb-service.llmops.svc.cluster.local",
    )
    port = int(os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    user = os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password = os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    marker = _marker_for_sql(identity.fdm_computed_at)
    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=identity.database,
        connect_timeout=5,
        read_timeout=120,
        write_timeout=30,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION READ ONLY")
            try:
                cursor.execute("SELECT @@session.tx_read_only AS tx_read_only")
            except pymysql.MySQLError:
                cursor.execute("SELECT @@session.transaction_read_only AS tx_read_only")
            tx_read_only = int(cursor.fetchone()["tx_read_only"])
            cursor.execute("SELECT DATABASE() AS database")
            database = str(cursor.fetchone()["database"])
            fdm_marker_rows = _fetch_all(
                cursor,
                """
                SELECT dimension_type, COUNT(*) AS row_count,
                       COUNT(DISTINCT computed_at) AS marker_count,
                       MIN(computed_at) AS computed_at_min,
                       MAX(computed_at) AS computed_at_max
                FROM mart_general_filter_dimension_metric
                WHERE source = 'ubist' AND measure = 'sales'
                  AND dimension_type IN (%s, %s, %s, %s, %s)
                GROUP BY dimension_type
                ORDER BY dimension_type
                """,
                SIDECAR_DIMENSIONS,
            )
            market_rows = _fetch_all(
                cursor,
                """
                SELECT atc4_code AS market_id, market_size_series
                FROM mart_general_market_metric
                WHERE source = 'ubist' AND measure = 'sales'
                ORDER BY atc4_code
                """,
            )
            with connection.cursor(pymysql.cursors.SSDictCursor) as stream_cursor:
                stream_cursor.execute(
                    """
                    SELECT atc4_code AS market_id, dimension_type, raw_value_history
                    FROM mart_general_filter_dimension_metric
                    WHERE source = 'ubist' AND measure = 'sales'
                      AND dimension_type IN (%s, %s, %s, %s, %s)
                      AND computed_at = %s
                    ORDER BY atc4_code, dimension_type, dimension_value_norm
                    """,
                    (*SIDECAR_DIMENSIONS, marker),
                )
                sidecar_rows = _aggregate_history_rows(stream_cursor)
            with connection.cursor(pymysql.cursors.SSDictCursor) as stream_cursor:
                stream_cursor.execute(
                    """
                    SELECT atc4_code AS market_id, dimension_type, raw_value_history
                    FROM mart_general_filter_dimension_metric
                    WHERE source = 'ubist' AND measure = 'sales'
                      AND dimension_type = 'molecule'
                    ORDER BY atc4_code, dimension_value_norm
                    """
                )
                molecule_rows = _aggregate_history_rows(stream_cursor)
        connection.rollback()
    finally:
        connection.close()
    return {
        "tx_read_only": tx_read_only,
        "database": database,
        "fdm_marker_rows": [
            {
                **row,
                "computed_at_min": _iso(row.get("computed_at_min")),
                "computed_at_max": _iso(row.get("computed_at_max")),
            }
            for row in fdm_marker_rows
        ],
        "market_rows": market_rows,
        "sidecar_rows": sidecar_rows,
        "molecule_rows": molecule_rows,
    }


def _authorization_failure(failures: Sequence[str]) -> dict[str, Any]:
    return {
        "gate": "mart_reload_authorization",
        "classification": "census",
        "checked": 0,
        "population": 1,
        "missing": "fail",
        "tolerance": "exact",
        "failures": list(failures),
        "failure_reasons": list(failures),
        "failure_count": len(failures),
        "exit_code": 1,
        "environment": "runtime_mart_read_only",
    }


def _authorized_identity(
    reload_run_id: str | None,
    database: str | None,
    fdm_computed_at: str | None,
) -> tuple[ReloadIdentity | None, list[str]]:
    values = {
        "reload_run_id": str(reload_run_id or "").strip(),
        "database": str(database or "").strip(),
        "fdm_computed_at": str(fdm_computed_at or "").strip(),
    }
    failures: list[str] = []
    for name, value in values.items():
        if not value:
            failures.append(f"{name}_missing")
    if values["reload_run_id"] and IDENTITY_RE.fullmatch(values["reload_run_id"]) is None:
        failures.append("reload_run_id_invalid")
    if values["database"] and DATABASE_RE.fullmatch(values["database"]) is None:
        failures.append("database_invalid")
    if values["fdm_computed_at"] and not _iso(values["fdm_computed_at"]):
        failures.append("fdm_computed_at_invalid")
    if failures:
        return None, failures
    return ReloadIdentity(**values), []


def _acceptance_line(gate: Mapping[str, Any]) -> str:
    keys = (
        "gate",
        "classification",
        "checked",
        "population",
        "missing",
        "tolerance",
        "failure_count",
        "exit_code",
        "environment",
    )
    return " ".join(f"{key}={gate[key]}" for key in keys)


def _print_report(report: Mapping[str, Any]) -> None:
    for gate in report["gates"]:
        print(_acceptance_line(gate))
        for failure in gate["failures"]:
            print(f"failure gate={gate['gate']} reason={failure}")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reload-run-id", default=os.environ.get("MART_RELOAD_RUN_ID"))
    parser.add_argument("--database", default=os.environ.get("MART_RELOAD_DB_NAME"))
    parser.add_argument("--fdm-computed-at", default=os.environ.get("MART_FDM_COMPUTED_AT"))
    args = parser.parse_args()
    identity, failures = _authorized_identity(
        args.reload_run_id,
        args.database,
        args.fdm_computed_at,
    )
    if args.evidence is None and os.environ.get("MART_RELOAD_COMPLETE") != "1":
        failures.insert(0, "reload_completion_not_authorized")
    if failures or identity is None:
        gate = _authorization_failure(failures or ["reload_identity_missing"])
        _print_report({"gates": [gate], "exit_code": 1})
        return 1
    evidence = (
        json.loads(args.evidence.read_text(encoding="utf-8"))
        if args.evidence is not None
        else collect_runtime_evidence(identity)
    )
    report = validate_evidence(evidence, identity)
    _print_report(report)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
