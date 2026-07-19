"""Read-only census comparison for isolated full-rehearsal outputs."""

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
    table_exists,
)


SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")
MART_PREFIX = "jw_mart_rehearsal_"
CACHE_PREFIX = "jw_mart_s6_rehearsal_"

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
    "mart_analysis_level_block",
)
CACHE_TABLES = (
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
    "cache_deep_analysis_general",
    "cache_market_forecast_general",
    "cache_brand_elements",
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


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    tables: tuple[TableComparison, ...]

    @property
    def failures(self) -> int:
        return sum(row.status != "match" for row in self.tables)

    @property
    def exit_code(self) -> int:
        return int(self.failures > 0)

    def as_dict(
        self,
        *,
        gate: str = "R-1",
        environment: str = "isolated-full-rehearsal",
        input_inventory_sha256: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "gate": gate,
            "classification": "census",
            "checked": len(self.tables),
            "population": len(MART_TABLES) + len(CACHE_TABLES),
            "missing": "fail",
            "tolerance": "exact-canonical-sha256",
            "failures": self.failures,
            "exit_code": self.exit_code,
            "environment": environment,
            "tables": [asdict(row) for row in self.tables],
        }
        if input_inventory_sha256 is not None:
            payload["input_inventory_sha256"] = input_inventory_sha256
        return payload


def compare_full_rehearsal(
    conn: pymysql.connections.Connection,
    config: ComparisonConfig,
) -> ComparisonReport:
    config.validate()
    rows = [
        _compare_table(conn, table, "mart", config.reference_db, config.target_db)
        for table in MART_TABLES
    ]
    rows.extend(
        _compare_table(
            conn,
            table,
            "cache",
            config.reference_cache_db,
            config.target_cache_db,
        )
        for table in CACHE_TABLES
    )
    return ComparisonReport(tuple(rows))


def run_comparison(
    config: ComparisonConfig,
    output: Path,
    *,
    gate: str = "R-1",
    environment: str = "isolated-full-rehearsal",
    input_inventory_sha256: str | None = None,
) -> int:
    conn = connect_admin()
    try:
        report = compare_full_rehearsal(conn, config)
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            report.as_dict(
                gate=gate,
                environment=environment,
                input_inventory_sha256=input_inventory_sha256,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return report.exit_code


def _compare_table(
    conn: pymysql.connections.Connection,
    table: str,
    family: Literal["mart", "cache"],
    reference_db: str,
    target_db: str,
) -> TableComparison:
    if not table_exists(conn, reference_db, table):
        return TableComparison(table, family, reference_db, target_db, "missing_reference", None, None)
    if not table_exists(conn, target_db, table):
        return TableComparison(table, family, reference_db, target_db, "missing_target", None, None)
    reference = canonical_reference_digest(conn, reference_db, table)
    target = canonical_reference_digest(conn, target_db, table)
    if reference.row_count != target.row_count:
        status: ComparisonStatus = "row_count_mismatch"
    elif reference.sha256 != target.sha256:
        status = "digest_mismatch"
    else:
        status = "match"
    return TableComparison(table, family, reference_db, target_db, status, reference, target)
