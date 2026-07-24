from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline.etl.io.enrich.iqvia_nsa_bridge import _iqvia_match_sql


def _write_nsa_fixture(path: Path, metric_value: int | float | str) -> None:
    row = {
        "source_file": "KOR_NSA_Jun-25-2026.xlsx",
        "sheet_name": "NSA",
        "source_row_no": 2,
        "audit_code": "KCPA",
        "period_label": "2026-Q1",
        "product_name_kor": "Drug",
        "product_name": "Drug",
        "pack_desc": "10MG",
        "atc4_code": "A01A",
        "values_lc": metric_value,
        "counting_units": metric_value,
        "units": metric_value,
        "dosage_units": metric_value,
    }
    pq.write_table(pa.Table.from_pylist([row]), path)


def _execute_match_sql(path: Path) -> tuple[float, float, float, float]:
    connection = duckdb.connect()
    connection.register(
        "iqvia_product_bridge",
        pd.DataFrame(
            [
                {
                    "product_id": "product-1",
                    "ml_id": "ml-1",
                    "match_key": "drug",
                    "brand_key": "drug",
                    "match_method": "product_name_pack",
                    "match_confidence": "high",
                    "priority": 1,
                }
            ]
        ),
    )
    sql = _iqvia_match_sql(
        nsa_glob=path.as_posix(),
        targets=[],
        atc_codes=[],
        ingested_at="2026-07-25T00:00:00+00:00",
    )
    try:
        row = connection.execute(sql).fetchone()
    finally:
        connection.close()
    assert row is not None
    return tuple(float(value) for value in row[4:8])


@pytest.mark.parametrize(
    ("metric_value", "expected"),
    [
        (1234, 1234.0),
        (1234.5, 1234.5),
        ("1,234", 1234.0),
    ],
)
def test_actual_iqvia_enrich_sql_accepts_numeric_and_comma_string_metrics(
    tmp_path: Path,
    metric_value: int | float | str,
    expected: float,
) -> None:
    source = tmp_path / "nsa.parquet"
    _write_nsa_fixture(source, metric_value)

    assert _execute_match_sql(source) == (expected, expected, expected, expected)


def test_legacy_string_only_expression_rejects_bigint_fixture(tmp_path: Path) -> None:
    source = tmp_path / "nsa.parquet"
    _write_nsa_fixture(source, 1234)

    connection = duckdb.connect()
    try:
        with pytest.raises(duckdb.BinderException, match=r"replace\(BIGINT"):
            connection.execute(
                "SELECT try_cast(replace(values_lc, ',', '') AS DOUBLE) "
                f"FROM read_parquet('{source.as_posix()}')"
            )
    finally:
        connection.close()
