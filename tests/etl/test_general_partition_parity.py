from __future__ import annotations

import duckdb
from decimal import Decimal
import json
import pandas as pd

from pipeline.etl.io.mart import general_compute
from pipeline.etl.io.mart.general_json import (
    assert_canonical_parity,
    canonicalize,
    canonical_rows_sha256,
)
from pipeline.etl.io.mart.general_rows import build_brand_rows, build_market_rows
from pipeline.etl.io.mart.general_ubist import iter_ubist_atc4_frames, load_ubist_base_frame


BRAND_KEY = ("brand_key", "atc4_code", "source", "measure")
MARKET_KEY = ("atc4_code", "source", "measure")


def _raw_row(
    *,
    product_code: str,
    brand: str,
    atc: str,
    period: str,
    sales: float,
    volume: float,
    channel: str = "의원",
    specialty: str = "가정의학과",
) -> dict[str, object]:
    return {
        "약품코드": product_code,
        "제품": f"{brand} {product_code}",
        "브랜드": brand,
        "ATC": atc,
        "period_yyyymm": period,
        "종별": channel,
        "진료과": specialty,
        "제조사": f"{brand} Maker",
        "판매사": f"{brand} Seller",
        "성분": "A / B",
        "성분용량": "10mg",
        "제형": "정제",
        "투여경로": "경구",
        "급여구분": "급여",
        "rx_amt": sales,
        "rx_qty": volume,
    }


def _fixture_rows() -> list[dict[str, object]]:
    rows = []
    for period, multiplier in (("2025-01", 1.0), ("2026-01", 2.0)):
        rows.extend(
            [
                _raw_row(
                    product_code="p1",
                    brand="Alpha",
                    atc="C10A1 Lipid",
                    period=period,
                    sales=10 * multiplier,
                    volume=2 * multiplier,
                ),
                _raw_row(
                    product_code="p2",
                    brand="Alpha",
                    atc="C10A1 Lipid",
                    period=period,
                    sales=5 * multiplier,
                    volume=1 * multiplier,
                    channel="종합병원",
                ),
                _raw_row(
                    product_code="p3",
                    brand="Beta",
                    atc="C10A1 Lipid",
                    period=period,
                    sales=15 * multiplier,
                    volume=3 * multiplier,
                    specialty="순환기내과",
                ),
                _raw_row(
                    product_code="p4",
                    brand="Gamma",
                    atc="A01A1 Other",
                    period=period,
                    sales=7 * multiplier,
                    volume=4 * multiplier,
                ),
            ]
        )
    return rows


def _outputs(base_frames: list[pd.DataFrame]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    brand_rows: list[dict[str, object]] = []
    market_rows: list[dict[str, object]] = []
    for frame in base_frames:
        for measure, value_column in (
            ("sales", "raw_sales_minor"),
            ("volume", "raw_volume_minor"),
        ):
            current_brands = build_brand_rows(
                "ubist",
                measure,
                frame,
                {},
                value_column=value_column,
                minor_unit_scale=Decimal("100"),
            )
            brand_rows.extend(current_brands)
            market_rows.extend(build_market_rows("ubist", measure, current_brands))
    return brand_rows, market_rows


def _run_fixture_parity(
    rows: list[dict[str, object]],
    monkeypatch,
    tmp_path,
    *,
    memory_budget_bytes: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.register("source", pd.DataFrame(rows))
        connection.execute(
            f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)"
        )
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))
    legacy_brand, legacy_market = _outputs([load_ubist_base_frame()])
    candidate_frames = [
        frame
        for _atc4, frame in iter_ubist_atc4_frames(
            spool_dir=tmp_path / "spool",
            memory_budget_bytes=memory_budget_bytes,
            estimated_row_bytes=512,
            target_fraction=0.25,
        )
    ]
    candidate_brand, candidate_market = _outputs(candidate_frames)
    assert_canonical_parity(
        legacy_brand,
        candidate_brand,
        sort_key=BRAND_KEY,
    )
    assert_canonical_parity(
        legacy_market,
        candidate_market,
        sort_key=MARKET_KEY,
    )
    return candidate_brand, candidate_market


def test_partition_path_matches_full_frame_for_split_brand_ties_dimensions_and_measures(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.register("source", pd.DataFrame(_fixture_rows()))
        connection.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))

    full_brand, full_market = _outputs([load_ubist_base_frame()])
    partitioned_frames = [
        frame
        for _atc4, frame in iter_ubist_atc4_frames(
            spool_dir=tmp_path / "spool",
            memory_budget_bytes=8192,
            estimated_row_bytes=512,
            target_fraction=0.25,
        )
    ]
    partition_brand, partition_market = _outputs(partitioned_frames)

    full_brand_rows, full_brand_hash = canonical_rows_sha256(full_brand, sort_key=BRAND_KEY)
    part_brand_rows, part_brand_hash = canonical_rows_sha256(partition_brand, sort_key=BRAND_KEY)
    full_market_rows, full_market_hash = canonical_rows_sha256(full_market, sort_key=MARKET_KEY)
    part_market_rows, part_market_hash = canonical_rows_sha256(partition_market, sort_key=MARKET_KEY)
    assert part_brand_rows == full_brand_rows
    assert part_brand_hash == full_brand_hash
    assert part_market_rows == full_market_rows
    assert part_market_hash == full_market_hash


def test_split_product_fixture_preserves_brand_across_product_buckets(
    monkeypatch,
    tmp_path,
) -> None:
    rows = [
        _raw_row(
            product_code=f"p{index}",
            brand="Alpha",
            atc="C10A1 Lipid",
            period="2026-01",
            sales=10.0 + index,
            volume=2.0 + index,
        )
        for index in range(1, 9)
    ]
    for row in rows:
        row["제품"] = "Alpha"

    candidate_brand, _candidate_market = _run_fixture_parity(
        rows,
        monkeypatch,
        tmp_path,
        memory_budget_bytes=16384,
    )

    assert {
        (row["brand_key"], row["measure"])
        for row in candidate_brand
    } == {("alpha", "sales"), ("alpha", "volume")}


def test_skew_fixture_activates_oversized_partition_and_preserves_parity(
    monkeypatch,
    tmp_path,
) -> None:
    rows = [
        _raw_row(
            product_code=f"p{index:04d}",
            brand=f"Brand {index:04d}",
            atc="M1A1 Skew",
            period="2026-01",
            sales=float(index + 1),
            volume=float(index + 2),
        )
        for index in range(128)
    ]

    _run_fixture_parity(
        rows,
        monkeypatch,
        tmp_path,
        memory_budget_bytes=8192,
    )

    manifest = json.loads(
        (tmp_path / "spool" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["partitions"][0]["bucket_count"] > 1


def test_exact_tie_fixture_is_deterministic_across_paths(
    monkeypatch,
    tmp_path,
) -> None:
    rows = [
        _raw_row(
            product_code="p-zeta",
            brand="Zeta",
            atc="C10A1 Lipid",
            period="2026-01",
            sales=10.0,
            volume=2.0,
        ),
        _raw_row(
            product_code="p-alpha",
            brand="Alpha",
            atc="C10A1 Lipid",
            period="2026-01",
            sales=10.0,
            volume=2.0,
        ),
    ]
    rows[0]["제품"] = "Zeta"
    rows[1]["제품"] = "Alpha"

    candidate_brand, _candidate_market = _run_fixture_parity(
        rows,
        monkeypatch,
        tmp_path / "forward",
        memory_budget_bytes=4096,
    )
    reversed_brand, _reversed_market = _run_fixture_parity(
        list(reversed(rows)),
        monkeypatch,
        tmp_path / "reversed",
        memory_budget_bytes=4096,
    )

    sales_rows = {
        row["brand_key"]: row
        for row in candidate_brand
        if row["measure"] == "sales"
    }
    reversed_sales_rows = {
        row["brand_key"]: row
        for row in reversed_brand
        if row["measure"] == "sales"
    }
    assert sales_rows["alpha"]["metric_history"]["2026-01"]["rank"] == 1
    assert sales_rows["zeta"]["metric_history"]["2026-01"]["rank"] == 2
    assert canonicalize(reversed_sales_rows) == canonicalize(sales_rows)


def test_canonical_sha_gate_rejects_one_omitted_partial() -> None:
    rows = [
        {"brand_key": "a", "atc4_code": "C10A1", "source": "ubist", "measure": "sales"},
        {"brand_key": "b", "atc4_code": "C10A1", "source": "ubist", "measure": "sales"},
    ]
    _all_rows, all_hash = canonical_rows_sha256(rows, sort_key=BRAND_KEY)
    _missing_rows, missing_hash = canonical_rows_sha256(rows[:-1], sort_key=BRAND_KEY)
    assert missing_hash != all_hash


def test_streamed_brand_stable_worksets_match_full_frame_sha(
    monkeypatch,
    tmp_path,
) -> None:
    raw_dir = tmp_path / "ubist" / "year=2026" / "month=01"
    raw_dir.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.register("source", pd.DataFrame(_fixture_rows()))
        connection.execute(f"COPY source TO '{raw_dir / 'data.parquet'}' (FORMAT PARQUET)")
    monkeypatch.setenv("S4_INPUT_MODE", "raw")
    monkeypatch.setenv("S4_UBIST_DIR", str(tmp_path / "ubist"))
    monkeypatch.setattr(general_compute, "load_catalog_key_map", lambda: {})

    full_brand, full_market = _outputs([load_ubist_base_frame()])
    output_dir = tmp_path / "output"
    general_compute.compute_general(
        "ubist",
        dry_run=True,
        output_dir=output_dir,
        spool_dir=tmp_path / "spool",
        memory_budget_bytes=24576,
    )
    partition_brand = [
        json.loads(line)
        for line in (output_dir / "general_v3_ubist_brand_rows.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    partition_market = [
        json.loads(line)
        for line in (output_dir / "general_v3_ubist_market_rows.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    full_brand_rows, full_brand_hash = canonical_rows_sha256(
        full_brand,
        sort_key=BRAND_KEY,
    )
    part_brand_rows, part_brand_hash = canonical_rows_sha256(
        partition_brand,
        sort_key=BRAND_KEY,
    )
    full_market_rows, full_market_hash = canonical_rows_sha256(
        full_market,
        sort_key=MARKET_KEY,
    )
    part_market_rows, part_market_hash = canonical_rows_sha256(
        partition_market,
        sort_key=MARKET_KEY,
    )
    assert part_brand_rows == full_brand_rows
    assert part_brand_hash == full_brand_hash
    assert part_market_rows == full_market_rows
    assert part_market_hash == full_market_hash
