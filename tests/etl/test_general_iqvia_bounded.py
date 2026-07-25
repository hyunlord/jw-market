from __future__ import annotations

from pathlib import Path

import pandas as pd

from pipeline.etl.io.mart.general_iqvia import (
    _raw_scope_sql,
    iter_iqvia_base_frames,
    load_iqvia_base_frame,
)


def _write_parquet_fixture(root: Path) -> tuple[Path, Path]:
    nsa_dir = root / "iqvia_nsa"
    enriched_dir = root / "enriched" / "ml_id=ml_001"
    nsa_dir.mkdir(parents=True)
    enriched_dir.mkdir(parents=True)

    nsa_rows = []
    enriched_rows = []
    for index, (period, atc4, sales) in enumerate(
        (
            ("2025-Q4", "A01A", 10),
            ("2026-Q1", "A01A", 20),
            ("2026-Q1", "C10A", 30),
        ),
        start=1,
    ):
        source_file = "KOR_NSA_Jun-25-2026.xlsx"
        source_row_no = index + 1
        audit_code = "KCPA"
        nsa_rows.append(
            {
                "source_file": source_file,
                "sheet_name": "NSA",
                "source_row_no": source_row_no,
                "audit_code": audit_code,
                "period_label": period,
                "product_name_kor": f"제품{index}",
                "product_name": f"PRODUCT {index}",
                "pack_desc": "10MG",
                "strength": "10MG",
                "molecule_desc": "MOLECULE",
                "molecule_type": "SINGLE",
                "nfc3_desc": "TABLET",
                "nfc2_desc": "ORAL",
                "nfc1_desc": "SOLID",
                "nhi_type": "RX",
                "mfr_name_kor": "제조사",
                "mfr_name": "MAKER",
                "atc4_code": atc4,
                "atc4_desc": f"{atc4} DESC",
                "values_lc": sales,
                "units": sales / 2,
                "dosage_units": sales / 4,
                "counting_units": sales / 5,
            }
        )
        enriched_rows.append(
            {
                "product_id": f"p{index}",
                "source": "nsa",
                "period_yyyymm": period,
                "channel": "KCPA",
                "specialty": "",
                "raw_rx_amt": sales,
                "raw_rx_cnt": sales / 5,
                "raw_rx_qty": sales / 2,
                "source_row_id": (
                    f"nsa::{source_file}::NSA::{source_row_no}::{audit_code}::{period}"
                ),
            }
        )

    pd.DataFrame(nsa_rows).to_parquet(nsa_dir / "fixture.parquet", index=False)
    pd.DataFrame(enriched_rows).to_parquet(enriched_dir / "data.parquet", index=False)
    return nsa_dir, root / "enriched"


def _sort(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["period_yyyymm", "atc4_code", "product_id"]).reset_index(drop=True)


def test_enriched_quarter_and_atc4_filter_matches_full_subset(
    monkeypatch, tmp_path: Path
) -> None:
    nsa_dir, enriched_dir = _write_parquet_fixture(tmp_path)
    monkeypatch.setenv("S4_INPUT_MODE", "enriched")
    monkeypatch.setenv("S4_IQVIA_NSA_DIR", str(nsa_dir))
    monkeypatch.setenv("S4_ENRICHED_DIR", str(enriched_dir))
    monkeypatch.setattr(
        "pipeline.etl.io.mart.general_iqvia._attach_catalog",
        lambda frame: frame,
    )

    full = load_iqvia_base_frame()
    scoped = load_iqvia_base_frame(
        quarters=("2026-Q1",),
        atc4_codes=("C10A",),
    )
    expected = full.loc[
        (full["period_yyyymm"] == "2026-Q1") & (full["atc4_code"] == "C10A")
    ].copy()

    pd.testing.assert_frame_equal(_sort(scoped), _sort(expected), check_dtype=False)


def test_bounded_atc4_reader_matches_scoped_bulk_and_is_deterministic(
    monkeypatch, tmp_path: Path
) -> None:
    nsa_dir, enriched_dir = _write_parquet_fixture(tmp_path)
    monkeypatch.setenv("S4_INPUT_MODE", "enriched")
    monkeypatch.setenv("S4_IQVIA_NSA_DIR", str(nsa_dir))
    monkeypatch.setenv("S4_ENRICHED_DIR", str(enriched_dir))
    monkeypatch.setattr(
        "pipeline.etl.io.mart.general_iqvia._attach_catalog",
        lambda frame: frame,
    )

    bulk = load_iqvia_base_frame(quarters=("2026-Q1",))
    first = list(iter_iqvia_base_frames(quarters=("2026-Q1",)))
    second = list(iter_iqvia_base_frames(quarters=("2026-Q1",)))

    assert [atc4 for atc4, _frame in first] == ["A01A", "C10A"]
    assert [atc4 for atc4, _frame in second] == ["A01A", "C10A"]
    bounded = pd.concat([frame for _atc4, frame in first], ignore_index=True)
    repeated = pd.concat([frame for _atc4, frame in second], ignore_index=True)
    pd.testing.assert_frame_equal(_sort(bounded), _sort(bulk), check_dtype=False)
    pd.testing.assert_frame_equal(_sort(repeated), _sort(bounded), check_dtype=False)


def test_scope_rejects_invalid_quarter_before_read(
    monkeypatch, tmp_path: Path
) -> None:
    nsa_dir, enriched_dir = _write_parquet_fixture(tmp_path)
    monkeypatch.setenv("S4_INPUT_MODE", "enriched")
    monkeypatch.setenv("S4_IQVIA_NSA_DIR", str(nsa_dir))
    monkeypatch.setenv("S4_ENRICHED_DIR", str(enriched_dir))

    try:
        load_iqvia_base_frame(quarters=("2026-Q5",))
    except ValueError as exc:
        assert "invalid IQVIA quarter" in str(exc)
    else:
        raise AssertionError("invalid quarter must fail before reading data")


def test_raw_scope_is_parameterized_and_no_scope_keeps_legacy_query_shape() -> None:
    where, parameters = _raw_scope_sql(("2026-Q1",), ("C10A",))

    assert where.count("%s") == 2
    assert "2026-Q1" not in where
    assert "C10A" not in where
    assert parameters == ("2026Q1", "C10A")
    assert _raw_scope_sql((), ()) == ("", ())
