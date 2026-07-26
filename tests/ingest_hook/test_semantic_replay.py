"""Bounded business-data replay verification for Parquet roots."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from pipeline.scripts.ingest_hook.semantic_replay import (
    ReplayConfig,
    ReplayVerificationError,
    compare_parquet_roots,
    duckdb_session_statements,
    main,
)


def _write_parquet(path: Path, rows: list[tuple[int, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE source(product_id BIGINT, period VARCHAR, "
            "value VARCHAR, ingested_at VARCHAR)"
        )
        connection.executemany(
            "INSERT INTO source VALUES (?, ?, ?, ?)",
            [
                (product_id, period, value, f"run-{index}")
                for index, (product_id, period, value) in enumerate(rows)
            ],
        )
        connection.execute(
            "COPY source TO ? (FORMAT PARQUET)",
            [str(path)],
        )


def _config(tmp_path: Path) -> ReplayConfig:
    return ReplayConfig(
        business_columns=("product_id", "period", "value"),
        memory_limit="64MB",
        threads=1,
        temp_directory=tmp_path / "duckdb-tmp",
    )


def test_compare_matches_when_row_order_file_layout_and_metadata_differ(
    tmp_path: Path,
) -> None:
    # Given equivalent business rows with different order, file layout, and metadata.
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    rows = [
        (1, "2026-Q1", "100"),
        (2, "2026-Q1", "200"),
        (3, "2026-Q2", "300"),
    ]
    _write_parquet(expected / "all.parquet", rows)
    _write_parquet(actual / "part-b.parquet", [rows[2]])
    _write_parquet(actual / "part-a.parquet", [rows[1], rows[0]])

    # When the standalone bounded replay verifier compares explicit business columns.
    comparison = compare_parquet_roots(expected, actual, _config(tmp_path))

    # Then non-business serialization details do not affect the verdict.
    assert comparison.matches is True
    assert comparison.expected.row_count == 3
    assert comparison.actual.row_count == 3
    assert comparison.expected.fingerprint == comparison.actual.fingerprint
    assert comparison.expected.file_count == 1
    assert comparison.actual.file_count == 2


def test_compare_detects_business_value_difference(tmp_path: Path) -> None:
    # Given roots whose only difference is one business value.
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    _write_parquet(expected / "data.parquet", [(1, "2026-Q1", "100")])
    _write_parquet(actual / "data.parquet", [(1, "2026-Q1", "101")])

    # When the roots are compared.
    comparison = compare_parquet_roots(expected, actual, _config(tmp_path))

    # Then the verifier reports a business-data mismatch.
    assert comparison.matches is False
    assert comparison.expected.row_count == comparison.actual.row_count == 1
    assert comparison.expected.fingerprint != comparison.actual.fingerprint


def test_compare_fails_closed_when_business_column_is_missing(tmp_path: Path) -> None:
    # Given one root that lacks an explicitly contracted business column.
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    _write_parquet(expected / "data.parquet", [(1, "2026-Q1", "100")])
    actual.mkdir()
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE source(product_id BIGINT, period VARCHAR)")
        connection.execute("INSERT INTO source VALUES (1, '2026-Q1')")
        connection.execute(
            "COPY source TO ? (FORMAT PARQUET)",
            [str(actual / "data.parquet")],
        )

    # When the roots are compared, then the missing contract fails closed.
    with pytest.raises(ReplayVerificationError, match="missing business columns.*value"):
        compare_parquet_roots(expected, actual, _config(tmp_path))


def test_duckdb_plan_enforces_memory_threads_and_spill_directory(
    tmp_path: Path,
) -> None:
    # Given a bounded standalone verifier configuration.
    config = _config(tmp_path)

    # When its DuckDB session plan is rendered.
    statements = duckdb_session_statements(config)

    # Then every resource boundary is explicit before Parquet reads begin.
    assert statements == (
        "SET memory_limit = '64MB'",
        "SET threads = 1",
        f"SET temp_directory = '{(tmp_path / 'duckdb-tmp').as_posix()}'",
        "SET preserve_insertion_order = false",
    )
    assert all("read_parquet" not in statement for statement in statements)


def test_cli_returns_mismatch_without_materializing_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given two small roots with different business values.
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    _write_parquet(expected / "data.parquet", [(1, "2026-Q1", "100")])
    _write_parquet(actual / "data.parquet", [(1, "2026-Q1", "999")])

    # When the real CLI entry point runs.
    rc = main(
        [
            "--expected-root",
            str(expected),
            "--actual-root",
            str(actual),
            "--business-column",
            "product_id",
            "--business-column",
            "period",
            "--business-column",
            "value",
            "--memory-limit",
            "64MB",
            "--threads",
            "1",
            "--temp-directory",
            str(tmp_path / "duckdb-tmp"),
        ]
    )

    # Then mismatch is a stable nonzero verdict with bounded execution metadata.
    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert payload["matches"] is False
    assert payload["execution"]["mode"] == "standalone_bounded_job"
    assert payload["execution"]["memory_limit"] == "64MB"
    assert payload["execution"]["materialization"] == "aggregate_per_parquet_file"
