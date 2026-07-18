#!/usr/bin/env python3
"""Fail closed unless the promoted runtime mart passes seven independent censuses."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Final

try:
    from .post_reload_fdm_gate import (
        _authorization_failure,
        _authorized_identity,
        _print_report,
        collect_runtime_evidence as collect_fdm_evidence,
    )
    from .post_reload_fdm_contract import ReloadIdentity
    from .post_reload_mart_contract import (
        EXPECTED_SOURCE_TABLES,
        summarize_specialty_rows as _summarize_specialty_rows,
        validate_evidence,
    )
except ImportError:
    from post_reload_fdm_gate import (
        _authorization_failure,
        _authorized_identity,
        _print_report,
        collect_runtime_evidence as collect_fdm_evidence,
    )
    from post_reload_fdm_contract import ReloadIdentity
    from post_reload_mart_contract import (
        EXPECTED_SOURCE_TABLES,
        summarize_specialty_rows as _summarize_specialty_rows,
        validate_evidence,
    )


IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9_]+$")


def _quote_identifier(value: str) -> str:
    if IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace(" ", "T")
        if not text:
            return ""
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _collect_source_tables(cursor: Any, database: str) -> list[dict[str, Any]]:
    quoted_database = _quote_identifier(database)
    states: list[dict[str, Any]] = []
    for logical_name, table_name in EXPECTED_SOURCE_TABLES.items():
        quoted_table = _quote_identifier(table_name)
        cursor.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   MIN(computed_at) AS computed_at_min,
                   MAX(computed_at) AS computed_at_max
            FROM {quoted_database}.{quoted_table}
            WHERE source = %s AND measure = %s
            """,
            ("ubist", "sales"),
        )
        counts = dict(cursor.fetchone() or {})
        states.append(
            {
                "logical_name": logical_name,
                "table_schema": database,
                "table_name": table_name,
                "row_count": int(counts.get("row_count") or 0),
                "computed_at_min": _iso(counts.get("computed_at_min")),
                "computed_at_max": _iso(counts.get("computed_at_max")),
            }
        )
    return states


def collect_runtime_evidence(identity: ReloadIdentity) -> dict[str, Any]:
    """Collect FDM, source-table, and specialty evidence with read-only transactions."""

    import pymysql

    evidence = collect_fdm_evidence(identity)
    host = os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get(
        "CHAT_CACHE_DB_HOST",
        "llmops-mariadb-service.llmops.svc.cluster.local",
    )
    port = int(os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    user = os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password = os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
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
            source_tx_read_only = int(cursor.fetchone()["tx_read_only"])
            source_tables = _collect_source_tables(cursor, identity.database)
            with connection.cursor(pymysql.cursors.SSDictCursor) as stream_cursor:
                stream_cursor.execute(
                    """
                    SELECT atc4_code AS market_id, brand_name, metric_history, specialty_data
                    FROM mart_general_brand_metric
                    WHERE source = 'ubist' AND measure = 'sales'
                    ORDER BY atc4_code, brand_name, brand_key
                    """
                )
                general_specialty_summary = _summarize_specialty_rows(stream_cursor)
            with connection.cursor(pymysql.cursors.SSDictCursor) as stream_cursor:
                stream_cursor.execute(
                    """
                    SELECT ml_id AS market_id, brand_name, metric_history, specialty_data
                    FROM mart_strategic_ml_brand_metric
                    WHERE source = 'ubist' AND measure = 'sales'
                    ORDER BY ml_id, brand_name, brand_key
                    """
                )
                strategic_specialty_summary = _summarize_specialty_rows(
                    stream_cursor,
                    sparse_periods_are_zero=True,
                )
        connection.rollback()
    finally:
        connection.close()
    return {
        **evidence,
        "tx_read_only": int(int(evidence.get("tx_read_only") or 0) == 1 and source_tx_read_only == 1),
        "source_tables": source_tables,
        "general_specialty_summary": general_specialty_summary,
        "strategic_specialty_summary": strategic_specialty_summary,
    }


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
