from __future__ import annotations

import duckdb
import json
import pandas as pd
import pytest

from pipeline.etl.io.mart.general_ubist import _subpartition_count
from pipeline.etl.io.mart.general_ubist import _raw_ubist_filtered_query
from pipeline.etl.io.mart.general_ubist import _raw_product_bucket_count
from pipeline.etl.io.mart.general_ubist import _ordered_group_row_limit
from pipeline.etl.io.mart.general_ubist import DEFAULT_UBIST_DUCKDB_MEMORY_LIMIT
from pipeline.etl.io.mart.general_ubist import DECIMAL_ADDITIVE_CONTRACT
from pipeline.etl.io.mart.general_ubist import MINOR_UNIT_SCALE
from pipeline.etl.io.mart.general_ubist import assert_decimal_spool_schema
from pipeline.etl.io.mart.general_ubist import iter_ubist_atc4_frames
from pipeline.etl.io.mart.general_ubist import iter_ubist_base_frames
from pipeline.etl.io.mart.general_ubist import load_ubist_base_frame


def test_partitioned_raw_ubist_load_matches_bulk_result(monkeypatch, tmp_path) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    source = pd.DataFrame(
        [
            {
                "약품코드": "p1",
                "제품": "Product One",
                "브랜드": "Brand One",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "상급종병",
                "진료과": "내과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "A / B",
                "성분용량": "10mg",
                "제형": "정제",
                "투여경로": "경구",
                "급여구분": "급여",
                "rx_amt": 10.0,
                "rx_qty": 2.0,
            },
            {
                "약품코드": "p2",
                "제품": "Product Two",
                "브랜드": "Brand Two",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "가정의학과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "Vitamin B12",
                "성분용량": "20mg",
                "제형": "정제",
                "투여경로": "경구",
                "급여구분": "급여",
                "rx_amt": 20.0,
                "rx_qty": 4.0,
            },
        ]
    )
    con = duckdb.connect()
    try:
        con.register("source", source)
        con.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")
    finally:
        con.close()
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    bulk = load_ubist_base_frame().sort_values(["product_code", "period_yyyymm"]).reset_index(drop=True)
    partitioned = pd.concat(
        list(iter_ubist_base_frames(spool_dir=tmp_path / "spool", partition_count=2)),
        ignore_index=True,
    ).sort_values(["product_code", "period_yyyymm"]).reset_index(drop=True)

    pd.testing.assert_frame_equal(partitioned[bulk.columns], bulk, check_dtype=False)


def test_bulk_raw_ubist_load_configures_bounded_duckdb_spill(monkeypatch, tmp_path) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    source = pd.DataFrame(
        [
            {
                "약품코드": "p1",
                "제품": "Product One",
                "브랜드": "Brand One",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "가정의학과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "A",
                "성분용량": "10mg",
                "제형": "정제",
                "투여경로": "경구",
                "급여구분": "급여",
                "rx_amt": 10.0,
                "rx_qty": 2.0,
            }
        ]
    )
    real_connect = duckdb.connect
    with real_connect() as connection:
        connection.register("source", source)
        connection.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")

    statements: list[str] = []

    class RecordingConnection:
        def __init__(self) -> None:
            self._connection = real_connect()

        def execute(self, statement: str, parameters=None):
            statements.append(statement)
            if parameters is None:
                return self._connection.execute(statement)
            return self._connection.execute(statement, parameters)

        def close(self) -> None:
            self._connection.close()

    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))
    monkeypatch.setattr(
        "pipeline.etl.io.mart.general_ubist.duckdb.connect",
        lambda: RecordingConnection(),
    )

    load_ubist_base_frame()

    assert "SET memory_limit='4GB'" in statements
    assert "SET threads=2" in statements
    assert any(statement.startswith("SET temp_directory=") for statement in statements)


@pytest.mark.parametrize(
    ("estimated_bytes", "budget_bytes", "expected"),
    [
        (64, 1024, 1),
        (256, 1024, 1),
        (257, 1024, 2),
        (1025, 1024, 5),
    ],
)
def test_oversized_partition_count_is_derived_from_bytes(
    estimated_bytes: int,
    budget_bytes: int,
    expected: int,
) -> None:
    assert (
        _subpartition_count(
            estimated_bytes=estimated_bytes,
            memory_budget_bytes=budget_bytes,
            target_fraction=0.25,
        )
        == expected
    )


def test_global_aggregate_uses_bounded_two_gib_duckdb_limit() -> None:
    assert DEFAULT_UBIST_DUCKDB_MEMORY_LIMIT == "2GB"


def test_raw_product_partition_count_scales_with_memory_budget() -> None:
    source_bytes = 2_770_328_739

    eight_gib = _raw_product_bucket_count(
        source_bytes=source_bytes,
        memory_budget_bytes=8 * 1024**3,
    )
    four_gib = _raw_product_bucket_count(
        source_bytes=source_bytes,
        memory_budget_bytes=4 * 1024**3,
    )

    assert eight_gib == 517
    assert four_gib == 1033


def test_ordered_group_limit_is_budget_derived_and_has_fixture_floor() -> None:
    assert _ordered_group_row_limit(8 * 1024**3) == 2_097_152
    assert _ordered_group_row_limit(512) == 1024


def test_atc4_spool_reuses_python_extractor_and_reduces_product_stable_parts(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    rows = []
    for product_code, atc_text, sales in (
        ("p1", "[C10A1] Standard", 10.0),
        ("p2", "C10A1_Alternate", 20.0),
        ("p3", "nonstandard", 30.0),
        ("p4", None, 40.0),
    ):
        rows.append(
            {
                "약품코드": product_code,
                "제품": f"Product {product_code}",
                "브랜드": f"Brand {product_code}",
                "ATC": atc_text,
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "가정의학과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "A",
                "성분용량": "10mg",
                "제형": "정제",
                "투여경로": "경구",
                "급여구분": "급여",
                "rx_amt": sales,
                "rx_qty": sales / 2,
            }
        )
    with duckdb.connect() as connection:
        connection.register("source", pd.DataFrame(rows))
        connection.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    partitioned = list(
        iter_ubist_atc4_frames(
            spool_dir=tmp_path / "spool",
            memory_budget_bytes=512,
            estimated_row_bytes=256,
            target_fraction=0.25,
        )
    )

    assert [atc for atc, _frame in partitioned] == ["C10A1", "NONSTA", "UNKNOWN"]
    c10 = partitioned[0][1].sort_values("product_code").reset_index(drop=True)
    assert c10["product_code"].tolist() == ["p1", "p2"]
    assert c10["raw_sales_minor"].tolist() == [1000, 2000]
    manifest = (tmp_path / "spool" / "manifest.json").read_text(encoding="utf-8")
    assert '"C10A1"' in manifest
    assert '"bucket_count": 4' in manifest


def test_atc4_spool_uses_exact_decimal_additive_contract(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    common = {
        "약품코드": "p1",
        "제품": "Product One",
        "브랜드": "Brand One",
        "ATC": "C10A1 Test",
        "period_yyyymm": "202601",
        "종별": "의원",
        "진료과": "가정의학과",
        "제조사": "Maker",
        "판매사": "Seller",
        "성분": "A",
        "성분용량": "10mg",
        "제형": "정제",
        "투여경로": "경구",
        "급여구분": "급여",
        "rx_qty": 1.0,
    }
    rows = [
        {**common, "rx_amt": value}
        for value in (1.0e8, 0.01, -1.0e8, 0.02)
    ]
    with duckdb.connect() as connection:
        connection.register("source", pd.DataFrame(rows))
        connection.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    legacy = load_ubist_base_frame()
    partitioned = list(
        iter_ubist_atc4_frames(
            spool_dir=tmp_path / "spool",
            memory_budget_bytes=512,
            estimated_row_bytes=256,
            target_fraction=0.25,
        )
    )[0][1]

    assert partitioned["raw_sales_minor"].tolist() == [3]
    assert partitioned["raw_sales_minor"].tolist() == legacy["raw_sales_minor"].tolist()
    manifest = json.loads(
        (tmp_path / "spool" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["aggregation_contract"] == DECIMAL_ADDITIVE_CONTRACT
    assert manifest["product_bucket_count"] >= 1
    aggregate_parts = sorted((tmp_path / "spool" / "aggregate-parts").glob("*.parquet"))
    raw_parts = sorted((tmp_path / "spool" / "parts").glob("**/*.parquet"))
    assert aggregate_parts
    assert raw_parts
    with duckdb.connect() as connection:
        for path in (aggregate_parts[0], raw_parts[0]):
            assert_decimal_spool_schema(connection, str(path))


def test_atc4_spool_returns_empty_plan_when_all_values_are_nonpositive(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    source = pd.DataFrame(
        [
            {
                "약품코드": "p1",
                "제품": "Product One",
                "브랜드": "Brand One",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "내과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "Molecule",
                "성분용량": "10mg",
                "제형": "Tablet",
                "투여경로": "Oral",
                "급여구분": "Covered",
                "rx_amt": 0.0,
                "rx_qty": -1.0,
            }
        ]
    )
    with duckdb.connect() as connection:
        connection.register("source", source)
        connection.execute(
            f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
        )
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    partitioned = list(iter_ubist_atc4_frames(spool_dir=tmp_path / "spool"))

    assert partitioned == []
    manifest = json.loads(
        (tmp_path / "spool" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["partitions"] == []


def test_raw_loader_rejects_source_scale_residual_before_sum(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    source = pd.DataFrame(
        [
            {
                "약품코드": "p1",
                "제품": "Product One",
                "브랜드": "Brand One",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "내과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "Molecule",
                "성분용량": "10mg",
                "제형": "Tablet",
                "투여경로": "Oral",
                "급여구분": "Covered",
                "rx_amt": 10.000002,
                "rx_qty": 1.0,
            }
        ]
    )
    with duckdb.connect() as connection:
        connection.register("source", source)
        connection.execute(
            f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
        )
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    with pytest.raises(ValueError, match="source-scale gate"):
        load_ubist_base_frame()


def test_atc4_spool_preserves_deterministic_representative_row(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    common = {
        "약품코드": "p1",
        "브랜드": "Brand One",
        "ATC": "C10A1 Test",
        "period_yyyymm": "202601",
        "종별": "의원",
        "진료과": "가정의학과",
        "제조사": "Maker",
        "판매사": "Seller",
        "성분": "A",
        "성분용량": "10mg",
        "제형": "정제",
        "투여경로": "경구",
        "급여구분": "급여",
        "rx_amt": 10.0,
        "rx_qty": 2.0,
    }
    with duckdb.connect() as connection:
        source = pd.DataFrame(
            [
                {**common, "제품": "Canonical Product"},
                {**common, "제품": "Later Product"},
            ]
        )
        connection.register("source", source)
        connection.execute(
            f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
        )
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    partitioned = list(
        iter_ubist_atc4_frames(
            spool_dir=tmp_path / "spool",
            memory_budget_bytes=512,
        )
    )

    assert partitioned[0][1].iloc[0]["product_name"] == "Canonical Product"


def test_raw_spool_carries_source_order_candidate() -> None:
    query = _raw_ubist_filtered_query()

    assert "filename AS __source_file" in query
    assert "file_row_number AS __source_row" in query


def test_enriched_loader_preserves_minor_units_until_reduce(
    monkeypatch,
    tmp_path,
) -> None:
    from pipeline.etl.io.mart import general_ubist

    enriched_dir = tmp_path / "enriched" / "ml_id=ml_test"
    enriched_dir.mkdir(parents=True)
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.execute(
            f"""
            COPY (
              SELECT 'ml_test' AS ml_id,
                     'product-1' AS product_id,
                     'a::b::c::d::e::p1' AS source_row_id,
                     '202601' AS period_yyyymm,
                     '의원' AS channel,
                     '내과' AS specialty,
                     'ubist' AS source,
                     CAST(10.25 AS DOUBLE) AS raw_rx_amt,
                     CAST(3.50 AS DOUBLE) AS raw_rx_qty
            ) TO '{enriched_dir / "data.parquet"}' (FORMAT PARQUET)
            """
        )
        connection.execute(
            f"""
            COPY (
              SELECT 'p1' AS 약품코드,
                     'C10A1 Test' AS ATC,
                     'Molecule' AS 성분,
                     '10mg' AS 성분용량,
                     'Tablet' AS 제형,
                     'Oral' AS 투여경로,
                     'Covered' AS 급여구분
            ) TO '{raw_dir / "data.parquet"}' (FORMAT PARQUET)
            """
        )
    monkeypatch.setenv("S4_INPUT_MODE", "enriched")
    monkeypatch.setenv("S4_ENRICHED_DIR", str(tmp_path / "enriched"))
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))
    monkeypatch.setattr(
        general_ubist,
        "_attach_catalog",
        lambda frame: frame.assign(
            brand_name="Brand One",
            brand_key="brandone",
            catalog_atc4_code="C10A1",
            manufacturer="Maker",
            company="Seller",
            product_name="Product One",
        ),
    )

    frame = load_ubist_base_frame(ml="ml_test")

    assert frame["raw_sales_minor"].tolist() == [1025]
    assert frame["raw_volume_minor"].tolist() == [350]
    assert frame["display_priority_value_minor"].tolist() == [1025]
    assert frame["raw_sales_minor"].dtype == "int64"
    assert frame["raw_volume_minor"].dtype == "int64"
    assert "raw_sales" not in frame
    assert "raw_volume" not in frame
    assert MINOR_UNIT_SCALE == 100


def test_raw_loader_rejects_malformed_value_before_positive_filter(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    source = pd.DataFrame(
        [
            {
                "약품코드": "p1",
                "제품": "Product One",
                "브랜드": "Brand One",
                "ATC": "C10A1 Test",
                "period_yyyymm": "202601",
                "종별": "의원",
                "진료과": "내과",
                "제조사": "Maker",
                "판매사": "Seller",
                "성분": "Molecule",
                "성분용량": "10mg",
                "제형": "Tablet",
                "투여경로": "Oral",
                "급여구분": "Covered",
                "rx_amt": "not-a-number",
                "rx_qty": "0",
            }
        ]
    )
    with duckdb.connect() as connection:
        connection.register("source", source)
        connection.execute(
            f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
        )
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    with pytest.raises(ValueError, match="cast_failures"):
        load_ubist_base_frame()


def test_enriched_loader_rejects_malformed_value_before_filter(
    monkeypatch,
    tmp_path,
) -> None:
    enriched_dir = tmp_path / "enriched" / "ml_id=ml_test"
    enriched_dir.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.execute(
            f"""
            COPY (
              SELECT 'ml_test' AS ml_id,
                     'product-1' AS product_id,
                     'a::b::c::d::e::p1' AS source_row_id,
                     '202601' AS period_yyyymm,
                     '의원' AS channel,
                     '내과' AS specialty,
                     'ubist' AS source,
                     'not-a-number' AS raw_rx_amt,
                     '0' AS raw_rx_qty
            ) TO '{enriched_dir / "data.parquet"}' (FORMAT PARQUET)
            """
        )
    monkeypatch.setenv("S4_INPUT_MODE", "enriched")
    monkeypatch.setenv("S4_ENRICHED_DIR", str(tmp_path / "enriched"))

    with pytest.raises(ValueError, match="cast_failures"):
        load_ubist_base_frame(ml="ml_test")


def test_atc4_scope_is_not_discarded_when_no_limit_is_set(
    monkeypatch,
    tmp_path,
) -> None:
    from pipeline.etl.io.mart import general_ubist

    plans = [
        general_ubist.UbistAtc4Partition("A10C1", 1, 1, 1, 0),
        general_ubist.UbistAtc4Partition("B2D1", 1, 1, 1, 1),
    ]
    monkeypatch.setattr(general_ubist, "_build_atc4_spool", lambda **_kwargs: plans)
    monkeypatch.setattr(
        general_ubist,
        "_build_atc4_workset",
        lambda **kwargs: kwargs["plan"],
    )

    selected = list(
        general_ubist.iter_ubist_atc4_worksets(
            spool_dir=tmp_path,
            atc4_scope=("A10C1",),
        )
    )

    assert [plan.atc4_code for plan in selected] == ["A10C1"]
