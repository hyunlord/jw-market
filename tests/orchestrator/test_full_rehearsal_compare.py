from __future__ import annotations

import json
from pathlib import Path

from pipeline.orchestrator import cli
from pipeline.orchestrator.full_rehearsal_compare import (
    CACHE_TABLES,
    MART_TABLES,
    ComparisonConfig,
    ComparisonReport,
    compare_full_rehearsal,
)
from pipeline.scripts.deploy.mart_load_verify import CanonicalDigest
from pipeline.scripts.deploy.mart_load_verify import (
    CANONICAL_ORDER_COLUMNS,
    CANONICAL_REFERENCE_COLUMNS,
)


def test_full_comparison_population_covers_serving_mart_and_cache_tables() -> None:
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
        "mart_analysis_level_block",
    )


def test_full_comparison_population_has_explicit_canonical_digest_contracts() -> None:
    volatile = {
        "id",
        "computed_at",
        "ingested_at",
        "updated_at",
        "built_at",
        "expires_at",
        "source_computed_at",
        "strength_generated_at",
        "stale_marked_at",
        "source_epoch",
    }

    for table in (*MART_TABLES, *CACHE_TABLES):
        assert table in CANONICAL_REFERENCE_COLUMNS
        assert table in CANONICAL_ORDER_COLUMNS
        assert not volatile.intersection(CANONICAL_REFERENCE_COLUMNS[table])
    assert CACHE_TABLES == (
        "cache_brands",
        "cache_market_status",
        "cache_cause",
        "cache_deep_analysis",
        "cache_deep_analysis_general",
        "cache_market_forecast_general",
        "cache_brand_elements",
    )


def test_comparison_report_adds_input_inventory_sha_only_when_supplied() -> None:
    report = ComparisonReport(())
    inventory_sha256 = "b" * 64

    assert "input_inventory_sha256" not in report.as_dict()
    assert (
        report.as_dict(input_inventory_sha256=inventory_sha256)["input_inventory_sha256"]
        == inventory_sha256
    )


def test_comparison_reports_missing_table_without_skipping_population(monkeypatch) -> None:
    missing = "mart_analysis_level_block"

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, db_name, table: not (db_name.startswith("jw_mart_rehearsal_") and table == missing),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.canonical_reference_digest",
        lambda _conn, _db_name, _table: CanonicalDigest(row_count=3, sha256="a" * 64),
    )

    report = compare_full_rehearsal(object(), _config())

    assert len(report.tables) == len(MART_TABLES) + len(CACHE_TABLES)
    assert report.failures == 1
    assert report.tables[MART_TABLES.index(missing)].status == "missing_target"
    assert report.exit_code == 1


def test_comparison_distinguishes_row_count_and_digest_mismatches(monkeypatch) -> None:
    def digest(_conn: object, db_name: str, table: str) -> CanonicalDigest:
        if table == "cache_cause" and db_name.startswith("jw_mart_s6_rehearsal_"):
            return CanonicalDigest(row_count=4, sha256="a" * 64)
        if table == "cache_deep_analysis" and db_name.startswith("jw_mart_s6_rehearsal_"):
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

    assert by_table["cache_cause"].status == "row_count_mismatch"
    assert by_table["cache_deep_analysis"].status == "digest_mismatch"
    assert report.failures == 2


def test_compare_full_cli_writes_fail_closed_json_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_compare.table_exists",
        lambda _conn, _db_name, table: table != "cache_cause",
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
    assert payload["checked"] == len(MART_TABLES) + len(CACHE_TABLES)
    assert payload["failures"] == 1
    assert payload["exit_code"] == 1


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
