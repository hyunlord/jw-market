from __future__ import annotations

from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from pipeline.etl.io.mart.general_json import (
    assert_canonical_parity,
    canonical_row_sha256,
)
from pipeline.etl.io.mart.general_rows import (
    assert_pre_reduce_minor_units,
    build_brand_market_state,
    build_brand_period_summary,
    build_ubist_additive_partial,
    reduce_ubist_additive_partials,
)
from pipeline.etl.io.mart.general_ubist import (
    DECIMAL_ADDITIVE_CONTRACT,
    assert_decimal_spool_schema,
    fetch_minor_unit_frame,
    validate_source_scale,
)
from pipeline.etl.io.mart.layer3_compute_market_metric import (
    compute_brand_ranking_stacked,
    compute_company_ranking_stacked,
)


def test_source_scale_gate_accepts_scale_two_noise_within_tolerance() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            """
            CREATE TABLE source(rx_amt DOUBLE, rx_qty DOUBLE);
            INSERT INTO source VALUES (10.0000004, 2.0), (19.9999996, 3.25)
            """
        )

        census = validate_source_scale(connection, "SELECT * FROM source")

    assert census == {
        "rows": 2,
        "sales_max_residual": pytest.approx(4e-7),
        "volume_max_residual": 0.0,
    }


def test_source_scale_gate_rejects_residual_above_one_micro_unit() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            """
            CREATE TABLE source(rx_amt DOUBLE, rx_qty DOUBLE);
            INSERT INTO source VALUES (10.000002, 2.0)
            """
        )

        with pytest.raises(ValueError, match="source-scale gate"):
            validate_source_scale(connection, "SELECT * FROM source")


def test_source_scale_gate_rejects_uncastable_non_null_values() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            """
            CREATE TABLE source(rx_amt VARCHAR, rx_qty VARCHAR);
            INSERT INTO source VALUES ('not-a-number', '2.00')
            """
        )

        with pytest.raises(ValueError, match="cast_failures"):
            validate_source_scale(connection, "SELECT * FROM source")


def test_source_scale_gate_rejects_values_too_large_for_exact_double_cents() -> None:
    with duckdb.connect() as connection:
        connection.execute(
            """
            CREATE TABLE source(rx_amt DOUBLE, rx_qty DOUBLE);
            INSERT INTO source VALUES (1000000000000000.0, 2.00)
            """
        )

        with pytest.raises(ValueError, match="unsafe_magnitude"):
            validate_source_scale(connection, "SELECT * FROM source")


def test_spool_schema_gate_rejects_float_additive_columns(tmp_path) -> None:
    decimal_path = tmp_path / "decimal.parquet"
    float_path = tmp_path / "float.parquet"
    with duckdb.connect() as connection:
        connection.execute(
            f"""
            COPY (
              SELECT CAST(10.25 AS DECIMAL(38,2)) AS raw_sales,
                     CAST(3.50 AS DECIMAL(38,2)) AS raw_volume
            ) TO '{decimal_path}' (FORMAT PARQUET)
            """
        )
        connection.execute(
            f"""
            COPY (
              SELECT CAST(10.25 AS DOUBLE) AS raw_sales,
                     CAST(3.50 AS DOUBLE) AS raw_volume
            ) TO '{float_path}' (FORMAT PARQUET)
            """
        )
        assert_decimal_spool_schema(connection, str(decimal_path))
        with pytest.raises(TypeError, match="DECIMAL"):
            assert_decimal_spool_schema(connection, str(float_path))


def test_decimal_spool_is_loaded_as_int64_minor_units_without_float(tmp_path) -> None:
    path = tmp_path / "decimal.parquet"
    with duckdb.connect() as connection:
        connection.execute(
            f"""
            COPY (
              SELECT 'p1' AS product_code,
                     CAST(10.25 AS DECIMAL(38,2)) AS raw_sales,
                     CAST(3.50 AS DECIMAL(38,2)) AS raw_volume
            ) TO '{path}' (FORMAT PARQUET)
            """
        )
        frame = fetch_minor_unit_frame(
            connection,
            f"SELECT * FROM read_parquet('{path}')",
        )

    assert frame["raw_sales_minor"].tolist() == [1025]
    assert frame["raw_volume_minor"].tolist() == [350]
    assert frame["raw_sales_minor"].dtype == "int64"
    assert frame["raw_volume_minor"].dtype == "int64"
    assert_pre_reduce_minor_units(
        frame,
        ("raw_sales_minor", "raw_volume_minor"),
    )


def test_pre_reduce_gate_rejects_float_conversion() -> None:
    frame = pd.DataFrame({"raw_sales_minor": [1025.0]})

    with pytest.raises(TypeError, match="pre-reduce float"):
        assert_pre_reduce_minor_units(frame, ("raw_sales_minor",))


def test_production_partial_seams_reject_float_conversion() -> None:
    valid = pd.DataFrame(
        {
            "atc4_code": ["C10A1"],
            "raw_sales_minor": pd.Series([1025], dtype="int64"),
            "raw_volume_minor": pd.Series([350], dtype="int64"),
        }
    )
    partial = build_ubist_additive_partial(valid)
    partial.frame["raw_sales_minor"] = partial.frame["raw_sales_minor"].astype(float)

    with pytest.raises(TypeError, match="pre-reduce float"):
        reduce_ubist_additive_partials("C10A1", [partial])

    invalid = valid.assign(raw_volume_minor=valid["raw_volume_minor"].astype(float))
    with pytest.raises(TypeError, match="pre-reduce float"):
        build_ubist_additive_partial(invalid)


def test_exact_rank_uses_brand_key_as_required_tie_break() -> None:
    frame = pd.DataFrame(
        [
            {
                "atc4_code": "C10A1",
                "period_yyyymm": "2026-01",
                "brand_key": "zeta",
                "raw_value_minor": 1000,
            },
            {
                "atc4_code": "C10A1",
                "period_yyyymm": "2026-01",
                "brand_key": "alpha",
                "raw_value_minor": 1000,
            },
        ]
    )
    summary = build_brand_period_summary(
        frame,
        value_column="raw_value_minor",
    )
    state = build_brand_market_state(
        [summary],
        value_column="raw_value_minor",
        minor_unit_scale=Decimal("100"),
    )

    assert state.rank_lookup[("C10A1", "2026-01", "alpha")] == 1
    assert state.rank_lookup[("C10A1", "2026-01", "zeta")] == 2


def test_tie_break_gate_rejects_missing_brand_key() -> None:
    frame = pd.DataFrame(
        {
            "atc4_code": ["C10A1"],
            "period_yyyymm": ["2026-01"],
            "raw_value_minor": [1000],
        }
    )

    with pytest.raises(KeyError, match="tie-break"):
        build_brand_period_summary(frame, value_column="raw_value_minor")


def test_derived_rank_ties_use_domain_keys() -> None:
    rows = [
        {
            "brand_key": "zeta",
            "brand_name": "Zeta",
            "raw_value_history": {"2026-01": 10.0},
            "metric_history": {"2026-01": {"raw_value": 10.0}},
            "by_dimension": {"company": "Zulu"},
        },
        {
            "brand_key": "alpha",
            "brand_name": "Alpha",
            "raw_value_history": {"2026-01": 10.0},
            "metric_history": {"2026-01": {"raw_value": 10.0}},
            "by_dimension": {"company": "Acme"},
        },
    ]

    brand_ranks = compute_brand_ranking_stacked(rows)["2026-01"]
    company_ranks = compute_company_ranking_stacked(rows)["2026-01"]

    assert [item["brand_key"] for item in brand_ranks] == ["alpha", "zeta"]
    assert [item["company"] for item in company_ranks] == ["Acme", "Zulu"]


def test_canonical_parity_gate_rejects_injected_mismatch() -> None:
    rows = [
        {
            "brand_key": "alpha",
            "atc4_code": "C10A1",
            "source": "ubist",
            "measure": "sales",
            "raw_value_history": {"2026-01": 10.0},
        }
    ]
    changed = [
        {
            **rows[0],
            "raw_value_history": {"2026-01": 10.01},
        }
    ]

    with pytest.raises(AssertionError, match="normalized parity"):
        assert_canonical_parity(
            rows,
            changed,
            sort_key=("brand_key", "atc4_code", "source", "measure"),
        )


def test_canonical_hash_applies_api_round_down_contract_to_derived_floats() -> None:
    left = {"brand_key": "sample", "hhi": 1905.689022531482}
    right = {"brand_key": "sample", "hhi": 1905.6890225314821}

    assert canonical_row_sha256(left) == canonical_row_sha256(right)


@pytest.mark.parametrize(
    ("left_value", "right_value"),
    [
        (386.125, 386.12499999999994),
        (40624.5, 40624.49999999999),
        (13.0016, 13.001599999999968),
    ],
)
def test_canonical_hash_stabilizes_api_round_down_boundary_noise(
    left_value: float,
    right_value: float,
) -> None:
    left = {"brand_key": "sample", "mat": left_value}
    right = {"brand_key": "sample", "mat": right_value}

    assert canonical_row_sha256(left) == canonical_row_sha256(right)


def test_canonical_hash_preserves_four_decimal_boundary_changes() -> None:
    left = {"brand_key": "sample", "hhi": 1905.6890}
    right = {"brand_key": "sample", "hhi": 1905.6891}

    assert canonical_row_sha256(left) != canonical_row_sha256(right)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_canonical_hash_rejects_non_finite_numbers(value: float | Decimal) -> None:
    with pytest.raises(ValueError, match="non-finite canonical number"):
        canonical_row_sha256({"brand_key": "sample", "hhi": value})


@pytest.mark.parametrize(
    "field",
    [
        "raw_value_history",
        "market_size_series",
        "channel_data",
        "specialty_data",
    ],
)
def test_canonical_hash_preserves_exact_additive_leaf_differences(field: str) -> None:
    left = {"brand_key": "sample", field: {"2026-01": 10.00009}}
    right = {"brand_key": "sample", field: {"2026-01": 10.0}}

    assert canonical_row_sha256(left) != canonical_row_sha256(right)


def test_decimal_contract_name_is_versioned() -> None:
    assert DECIMAL_ADDITIVE_CONTRACT == "decimal-additive-v1"
