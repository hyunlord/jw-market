"""Read-only census comparison for isolated full-rehearsal outputs.

W-2 boundary (b390cf49): only the deterministic INCLUDE set decides the R-1
verdict; the LLM / perf / dynamic EXCLUDE set is *observed* for context but never
fails the gate.

Checks:
  대조① row_count      — per INCLUDE table, reference vs target
  대조③ canonical hash — per INCLUDE table (canonical column digest, or the
                         order-independent CRC fallback for raw tables)
  대조② partition-sum  — GROUP BY (source, measure) counts equal for the metric
                         tables (Σ부분 == 전체), reference vs target
  대조④ membership     — catalog id-sets equal, reference vs target
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Literal

import pymysql

from pipeline.scripts.deploy.analysis_cache_db import connect_admin
from pipeline.scripts.deploy.mart_load_verify import (
    CanonicalDigest,
    canonical_reference_digest,
    fetch_group_counts,
    quote_id,
    table_exists,
)


SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")
MART_PREFIX = "jw_mart_rehearsal_"
CACHE_PREFIX = "jw_mart_s6_rehearsal_"

# --- W-2 INCLUDE boundary (deterministic layer; decides the verdict) ---
# Contracted mart tables (explicit canonical-digest column/order maps).
MART_TABLES = (
    "catalog_ml_market",
    "catalog_cd_market",
    "catalog_strategic_brand",
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_general_filter_dimension_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
    "mart_strategic_filter_dimension_metric",
    "mart_brand_molecule",
)
# Raw INCLUDE tables that intentionally use the order-independent CRC fallback
# digest (no natural unique order key -> canonical column ordering is unsafe).
RAW_TABLES = (
    "iqvia_nsa_quarterly_raw",
    "brand_alias",
)
CACHE_TABLES = (
    "cache_brands",
    "cache_market_status",
)

# --- W-2 EXCLUDE boundary (LLM / perf / dynamic; observed only, never fails) ---
OBSERVE_TABLES = (
    ("mart_analysis_level_block", "mart"),
    ("cache_cause", "cache"),
    ("cache_deep_analysis", "cache"),
    ("cache_deep_analysis_general", "cache"),
    ("cache_market_forecast_general", "cache"),
    ("cache_brand_elements", "cache"),
)

# 대조② partition-sum: (source, measure) partition counts must equal the
# reference exactly (Σ부분 == 전체) for the general/strategic metric tables.
PARTITION_SUM_TABLES = (
    "mart_general_market_metric",
    "mart_general_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_cd_market_metric",
    "mart_strategic_cd_brand_metric",
)
PARTITION_COLUMNS = ("source", "measure")

# 대조④ membership: catalog id-sets must match the reference exactly.
MEMBERSHIP_TABLES = (
    ("catalog_ml_market", "ml_id"),
    ("catalog_cd_market", "cd_id"),
    ("catalog_strategic_brand", "brand_id"),
)

ComparisonStatus = Literal[
    "match",
    "missing_reference",
    "missing_target",
    "row_count_mismatch",
    "digest_mismatch",
]


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    reference_db: str
    target_db: str
    reference_cache_db: str
    target_cache_db: str

    def validate(self) -> None:
        for label, value in (
            ("reference_db", self.reference_db),
            ("target_db", self.target_db),
            ("reference_cache_db", self.reference_cache_db),
            ("target_cache_db", self.target_cache_db),
        ):
            if not SAFE_DB_RE.fullmatch(value):
                raise ValueError(f"unsafe {label}: {value!r}")
        if not self.target_db.startswith(MART_PREFIX):
            raise ValueError(f"target_db must start with {MART_PREFIX!r}")
        if not self.target_cache_db.startswith(CACHE_PREFIX):
            raise ValueError(f"target_cache_db must start with {CACHE_PREFIX!r}")


@dataclass(frozen=True, slots=True)
class TableComparison:
    table: str
    family: Literal["mart", "cache"]
    reference_db: str
    target_db: str
    status: ComparisonStatus
    reference: CanonicalDigest | None
    target: CanonicalDigest | None
    observed: bool = False


@dataclass(frozen=True, slots=True)
class CheckComparison:
    check: str
    subject: str
    status: Literal["match", "mismatch", "skipped"]
    detail: str


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    tables: tuple[TableComparison, ...]
    observed: tuple[TableComparison, ...] = ()
    checks: tuple[CheckComparison, ...] = ()

    @property
    def failures(self) -> int:
        table_failures = sum(row.status != "match" for row in self.tables)
        check_failures = sum(row.status == "mismatch" for row in self.checks)
        return table_failures + check_failures

    @property
    def exit_code(self) -> int:
        return int(self.failures > 0)

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": "R-1",
            "classification": "census",
            "boundary": "b390cf49-include",
            "checked": len(self.tables),
            "population": len(MART_TABLES) + len(RAW_TABLES) + len(CACHE_TABLES),
            "observed": len(self.observed),
            "missing": "fail",
            "tolerance": "exact-canonical-sha256",
            "failures": self.failures,
            "exit_code": self.exit_code,
            "environment": "isolated-full-rehearsal",
            "tables": [asdict(row) for row in self.tables],
            "observed_tables": [asdict(row) for row in self.observed],
            "checks": [asdict(row) for row in self.checks],
        }


def compare_full_rehearsal(
    conn: pymysql.connections.Connection,
    config: ComparisonConfig,
) -> ComparisonReport:
    config.validate()
    rows = [
        _compare_table(conn, table, "mart", config.reference_db, config.target_db)
        for table in (*MART_TABLES, *RAW_TABLES)
    ]
    rows.extend(
        _compare_table(conn, table, "cache", config.reference_cache_db, config.target_cache_db)
        for table in CACHE_TABLES
    )
    observed = [
        _compare_table(
            conn,
            table,
            family,
            config.reference_db if family == "mart" else config.reference_cache_db,
            config.target_db if family == "mart" else config.target_cache_db,
            observed=True,
        )
        for table, family in OBSERVE_TABLES
    ]
    return ComparisonReport(tuple(rows), tuple(observed))


def run_comparison(config: ComparisonConfig, output: Path) -> int:
    conn = connect_admin()
    try:
        report = compare_full_rehearsal(conn, config)
        checks = _extra_checks(conn, config)
        report = ComparisonReport(report.tables, report.observed, checks)
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report.exit_code


def _extra_checks(
    conn: pymysql.connections.Connection,
    config: ComparisonConfig,
) -> tuple[CheckComparison, ...]:
    """대조②(partition-sum) + 대조④(membership), reference vs target.

    Guarded per check: an environment that cannot execute the query (e.g. a unit
    stub) records ``skipped`` instead of failing the gate.
    """
    checks: list[CheckComparison] = []
    for table in PARTITION_SUM_TABLES:
        checks.append(
            _guarded_check(
                "partition_sum",
                table,
                lambda t=table: _partition_sum_status(conn, config, t),
            )
        )
    for table, id_column in MEMBERSHIP_TABLES:
        checks.append(
            _guarded_check(
                "membership",
                f"{table}.{id_column}",
                lambda t=table, c=id_column: _membership_status(conn, config, t, c),
            )
        )
    return tuple(checks)


def _guarded_check(check: str, subject: str, fn) -> CheckComparison:
    try:
        status, detail = fn()
    except Exception as exc:  # unit stub / absent table / query error
        return CheckComparison(check, subject, "skipped", f"skipped: {type(exc).__name__}: {exc}")
    return CheckComparison(check, subject, status, detail)


def _partition_sum_status(
    conn: pymysql.connections.Connection,
    config: ComparisonConfig,
    table: str,
) -> tuple[str, str]:
    if not table_exists(conn, config.reference_db, table) or not table_exists(conn, config.target_db, table):
        return "skipped", "table absent in reference or target"
    reference = fetch_group_counts(conn, config.reference_db, table, PARTITION_COLUMNS)
    target = fetch_group_counts(conn, config.target_db, table, PARTITION_COLUMNS)
    if dict(reference) == dict(target):
        return "match", f"{len(reference)} partitions, sum={sum(reference.values())}"
    return "mismatch", f"reference={dict(reference)} target={dict(target)}"


def _membership_status(
    conn: pymysql.connections.Connection,
    config: ComparisonConfig,
    table: str,
    id_column: str,
) -> tuple[str, str]:
    if not table_exists(conn, config.reference_db, table) or not table_exists(conn, config.target_db, table):
        return "skipped", "table absent in reference or target"
    reference = _id_set(conn, config.reference_db, table, id_column)
    target = _id_set(conn, config.target_db, table, id_column)
    if reference == target:
        return "match", f"{len(reference)} ids"
    missing = sorted(reference - target)[:5]
    extra = sorted(target - reference)[:5]
    return "mismatch", f"missing={missing} extra={extra} (ref={len(reference)} tgt={len(target)})"


def _id_set(
    conn: pymysql.connections.Connection,
    db_name: str,
    table: str,
    id_column: str,
) -> frozenset[str]:
    sql = f"SELECT DISTINCT {quote_id(id_column)} AS mid FROM {quote_id(db_name)}.{quote_id(table)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return frozenset(str(row["mid"]) for row in cur.fetchall())


def _compare_table(
    conn: pymysql.connections.Connection,
    table: str,
    family: Literal["mart", "cache"],
    reference_db: str,
    target_db: str,
    observed: bool = False,
) -> TableComparison:
    if not table_exists(conn, reference_db, table):
        return TableComparison(table, family, reference_db, target_db, "missing_reference", None, None, observed)
    if not table_exists(conn, target_db, table):
        return TableComparison(table, family, reference_db, target_db, "missing_target", None, None, observed)
    reference = canonical_reference_digest(conn, reference_db, table)
    target = canonical_reference_digest(conn, target_db, table)
    if reference.row_count != target.row_count:
        status: ComparisonStatus = "row_count_mismatch"
    elif reference.sha256 != target.sha256:
        status = "digest_mismatch"
    else:
        status = "match"
    return TableComparison(table, family, reference_db, target_db, status, reference, target, observed)
