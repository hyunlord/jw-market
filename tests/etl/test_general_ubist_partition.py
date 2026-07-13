from __future__ import annotations

import duckdb
import pandas as pd

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
