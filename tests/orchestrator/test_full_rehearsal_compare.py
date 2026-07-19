from __future__ import annotations

import json
from pathlib import Path

from pipeline.orchestrator import cli
from pipeline.orchestrator import full_rehearsal_compare as compare_mod
from pipeline.orchestrator.full_rehearsal_compare import (
    CACHE_TABLES,
    MART_TABLES,
    OBSERVE_TABLES,
    RAW_TABLES,
    ComparisonConfig,
    _membership_status,
    _partition_sum_status,
    compare_full_rehearsal,
)
from pipeline.scripts.deploy.mart_load_verify import CanonicalDigest
from pipeline.scripts.deploy.mart_load_verify import (
    CANONICAL_ORDER_COLUMNS,
    CANONICAL_REFERENCE_COLUMNS,
)


INCLUDE = (*MART_TABLES, *RAW_TABLES, *CACHE_TABLES)


def test_include_boundary_is_the_w2_deterministic_set() -> None:
    # W-2 INCLUDE (b390cf49): 14 mart/raw + 2 cache = 16 deterministic tables.
    assert MART_TABLES == (
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
    assert RAW_TABLES == ("iqvia_nsa_quarterly_raw", "brand_alias")
    assert CACHE_TABLES == ("cache_brands", "cache_market_status")
    assert len(INCLUDE) == 16
    # EXCLUDE (perf / LLM / dynamic) is observed only, never in the verdict set.
    observed = {name for name, _family in OBSERVE_TABLES}
    assert "mart_analysis_level_block" in observed
    assert observed.isdisjoint(set(INCLUDE))


def test_contracted_include_tables_have_explicit_canonical_digest_contracts() -> None:
    # Contracted mart + cache tables must carry explicit canonical maps.
    # RAW_TABLES intentionally use the order-independent CRC fallback.
    for table in (*MART_TABLES, *CACHE_TABLES):
        assert table in CANONICAL_REFERENCE_COLUMNS
        assert table in CANONICAL_ORDER_COLUMNS
    for table in RAW_TABLES:
        assert table not in CANONICAL_REFERENCE_COLUMNS


def test_comparison_reports_missing_table_without_skipping_population(monkeypatch) -> None:
    missing = "mart_brand_molecule"

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, db_name, table: not (db_name.startswith("jw_mart_rehearsal_") and table == missing),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.canonical_reference_digest",
        lambda _conn, _db_name, _table: CanonicalDigest(row_count=3, sha256="a" * 64),
    )

    report = compare_full_rehearsal(object(), _config())

    assert len(report.tables) == len(INCLUDE)
    assert len(report.observed) == len(OBSERVE_TABLES)
    assert report.failures == 1
    assert report.tables[INCLUDE.index(missing)].status == "missing_target"
    assert report.exit_code == 1


def test_observed_tables_never_fail_the_gate(monkeypatch) -> None:
    # An observed (EXCLUDE) table that differs must NOT raise failures.
    def digest(_conn: object, db_name: str, table: str) -> CanonicalDigest:
        if table == "cache_brand_elements" and db_name.startswith("jw_mart_s6_rehearsal_"):
            return CanonicalDigest(row_count=999, sha256="z" * 64)
        return CanonicalDigest(row_count=3, sha256="a" * 64)

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, _db_name, _table: True,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.canonical_reference_digest",
        digest,
    )

    report = compare_full_rehearsal(object(), _config())
    observed = {row.table: row for row in report.observed}

    assert observed["cache_brand_elements"].status == "row_count_mismatch"
    assert observed["cache_brand_elements"].observed is True
    assert report.failures == 0
    assert report.exit_code == 0


def test_comparison_distinguishes_row_count_and_digest_mismatches(monkeypatch) -> None:
    def digest(_conn: object, db_name: str, table: str) -> CanonicalDigest:
        if table == "cache_brands" and db_name.startswith("jw_mart_s6_rehearsal_"):
            return CanonicalDigest(row_count=4, sha256="a" * 64)
        if table == "cache_market_status" and db_name.startswith("jw_mart_s6_rehearsal_"):
            return CanonicalDigest(row_count=3, sha256="b" * 64)
        return CanonicalDigest(row_count=3, sha256="a" * 64)

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, _db_name, _table: True,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.canonical_reference_digest",
        digest,
    )

    report = compare_full_rehearsal(object(), _config())
    by_table = {row.table: row for row in report.tables}

    assert by_table["cache_brands"].status == "row_count_mismatch"
    assert by_table["cache_market_status"].status == "digest_mismatch"
    assert report.failures == 2


def test_partition_sum_and_membership_status(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, _db_name, _table: True,
    )
    # partition-sum: equal group maps -> match, unequal -> mismatch.
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.fetch_group_counts",
        lambda _conn, db_name, _table, _cols: {("ubist", "sales"): 361, ("iqvia_nsa", "sales"): 539}
        if db_name.startswith("jw_mart_d2") or db_name.startswith("jw_mart_rehearsal_")
        else {},
    )
    status, _ = _partition_sum_status(object(), _config(), "mart_general_market_metric")
    assert status == "match"

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.fetch_group_counts",
        lambda _conn, db_name, _table, _cols: {("ubist", "sales"): 361}
        if db_name.startswith("jw_mart_d2")
        else {("ubist", "sales"): 360},
    )
    status, _ = _partition_sum_status(object(), _config(), "mart_general_market_metric")
    assert status == "mismatch"

    # membership: equal id-sets -> match, unequal -> mismatch.
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare._id_set",
        lambda _conn, db_name, _table, _col: frozenset({"m1", "m2"}),
    )
    status, _ = _membership_status(object(), _config(), "catalog_ml_market", "ml_id")
    assert status == "match"

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare._id_set",
        lambda _conn, db_name, _table, _col: frozenset({"m1", "m2"})
        if db_name.startswith("jw_mart_d2")
        else frozenset({"m1"}),
    )
    status, _ = _membership_status(object(), _config(), "catalog_ml_market", "ml_id")
    assert status == "mismatch"


def test_compare_full_cli_writes_fail_closed_json_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "comparison.json"
    missing = "mart_general_market_metric"
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, db_name, table: not (db_name.startswith("jw_mart_rehearsal_") and table == missing),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.canonical_reference_digest",
        lambda _conn, _db_name, _table: CanonicalDigest(row_count=3, sha256="a" * 64),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.connect_admin",
        lambda: _Connection(),
    )

    rc = cli.main(
        [
            "compare-full",
            "--reference-db",
            "jw_mart_d2_stage_20260630_r2",
            "--target-db",
            "jw_mart_rehearsal_r1_20260718",
            "--reference-cache-db",
            "jw_mart_d2_stage_20260630_r2",
            "--target-cache-db",
            "jw_mart_s6_rehearsal_r1_20260718",
            "--output",
            str(output),
        ]
    )

    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["classification"] == "census"
    assert payload["checked"] == len(INCLUDE)
    assert payload["failures"] == 1
    assert payload["exit_code"] == 1
    # extra checks are recorded (skipped under the unit stub connection).
    assert isinstance(payload["checks"], list) and payload["checks"]
    assert all(c["status"] == "skipped" for c in payload["checks"])


def _config() -> ComparisonConfig:
    return ComparisonConfig(
        reference_db="jw_mart_d2_stage_20260630_r2",
        target_db="jw_mart_rehearsal_r1_20260718",
        reference_cache_db="jw_mart_d2_stage_20260630_r2",
        target_cache_db="jw_mart_s6_rehearsal_r1_20260718",
    )


class _Connection:
    def close(self) -> None:
        return None
