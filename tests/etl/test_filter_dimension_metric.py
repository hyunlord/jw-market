from __future__ import annotations

import pandas as pd
import pytest

from pipeline.etl.io.mart import filter_dimension_metric as sidecar
from pipeline.etl.io.mart.filter_dimension_load import filter_dimension_table_ddl


def test_ubist_registry_exposes_raw_molecule_dimension() -> None:
    enabled = sidecar.enabled_dimension_specs("ubist")
    names = {spec.dimension_type for spec in enabled}

    assert names == {"atc3", "atc4", "seller", "molecule", "molecule_strength", "form", "route", "reimbursement"}
    assert sidecar.DIMENSION_REGISTRY["ubist"]["molecule"].enabled is True


def test_iqvia_registry_exposes_enabled_dimensions_and_excludes_pack() -> None:
    enabled = sidecar.enabled_dimension_specs("iqvia_nsa")
    names = {spec.dimension_type for spec in enabled}

    assert names == {"mfr", "molecule_type", "molecule_desc", "strength", "nhi"}
    assert sidecar.DIMENSION_REGISTRY["iqvia_nsa"]["molecule_desc"].enabled is True
    assert sidecar.DIMENSION_REGISTRY["iqvia_nsa"]["pack"].enabled is False


def test_dimension_value_normalization_collapses_whitespace_and_excludes_empty_values() -> None:
    assert sidecar.normalize_dimension_value("  전문   급여  ") == "전문 급여"
    assert sidecar.normalize_dimension_value("N/A") is None
    assert sidecar.normalize_dimension_value(" - ") is None


def test_build_filter_dimension_rows_keeps_ubist_product_level_grain() -> None:
    frame = pd.DataFrame(
        [
            {
                "source": "ubist",
                "measure": "sales",
                "atc4_code": "A10X0",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "product_code": "P1",
                "period_yyyymm": "2025-01",
                "raw_value": 100.0,
                "company": "Seller A",
                "ubist_molecule_raw": "  metformin / sitagliptin  ",
                "ubist_molecule_strength": "10mg",
                "ubist_form": "정제",
                "ubist_route": "경구",
                "ubist_reimbursement": "급여",
            },
            {
                "source": "ubist",
                "measure": "sales",
                "atc4_code": "A10X0",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "product_code": "P2",
                "period_yyyymm": "2025-01",
                "raw_value": 900.0,
                "company": "Seller A",
                "ubist_molecule_raw": "vitamin B12",
                "ubist_molecule_strength": "20mg",
                "ubist_form": "캡슐",
                "ubist_route": "경구",
                "ubist_reimbursement": "급여",
            },
        ]
    )

    rows = sidecar.build_filter_dimension_rows("ubist", "sales", frame)
    tablet = next(
        row
        for row in rows
        if row["dimension_type"] == "form" and row["dimension_value_norm"] == "정제"
    )
    atc3 = next(row for row in rows if row["dimension_type"] == "atc3" and row["product_code"] == "P1")
    atc4 = next(row for row in rows if row["dimension_type"] == "atc4" and row["product_code"] == "P1")
    compound = next(row for row in rows if row["dimension_type"] == "molecule" and row["product_code"] == "P1")

    assert tablet["product_code"] == "P1"
    assert tablet["raw_value_history"] == {"2025-01": 100.0}
    assert atc3["dimension_value_norm"] == "A10X"
    assert atc4["dimension_value_norm"] == "A10X0"
    assert compound["dimension_value"] == "metformin / sitagliptin"
    assert compound["dimension_value_norm"] == "metformin / sitagliptin"
    assert compound["raw_value_history"] == {"2025-01": 100.0}
    assert len([row for row in rows if row["dimension_type"] == "molecule" and row["product_code"] == "P1"]) == 1


def test_build_filter_dimension_rows_can_rebuild_only_raw_ubist_molecule() -> None:
    frame = pd.DataFrame(
        [
            {
                "atc4_code": "A10N1",
                "brand_key": "brand-a",
                "brand_name": "브랜드A",
                "product_code": "p1",
                "period_yyyymm": "202601",
                "raw_value": 10.0,
                "ubist_molecule_raw": "  vitamin B12 / folate  ",
                "ubist_molecule_strength": "B12 1MG",
            }
        ]
    )

    rows = sidecar.build_filter_dimension_rows(
        "ubist",
        "sales",
        frame,
        dimension_types={"molecule"},
    )

    assert [row["dimension_type"] for row in rows] == ["molecule"]
    assert rows[0]["dimension_value"] == "vitamin B12 / folate"


def test_build_filter_dimension_rows_keeps_iqvia_product_level_grain() -> None:
    frame = pd.DataFrame(
        [
            {
                "source": "iqvia_nsa",
                "measure": "sales",
                "atc4_code": "A10X0",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "product_code": "P1",
                "period_yyyymm": "2025-01",
                "raw_value": 125.0,
                "company": "MFR A",
                "manufacturer": "MFR A",
                "molecule_type": "Single",
                "strength": "10MG",
                "nhi_type": "급여",
                "molecule_desc": "CARTEOLOL",
                "pack_desc": "Excluded pack",
            },
            {
                "source": "iqvia_nsa",
                "measure": "sales",
                "atc4_code": "A10X0",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "product_code": "P2",
                "period_yyyymm": "2025-01",
                "raw_value": 875.0,
                "company": "MFR A",
                "manufacturer": "MFR A",
                "molecule_type": "Combination",
                "strength": "20MG",
                "nhi_type": "비급여",
                "molecule_desc": "DORZOLAMIDE",
                "pack_desc": "Excluded pack",
            },
        ]
    )

    rows = sidecar.build_filter_dimension_rows("iqvia_nsa", "sales", frame)
    strength = next(
        row
        for row in rows
        if row["dimension_type"] == "strength" and row["dimension_value_norm"] == "10MG"
    )
    molecule = next(
        row
        for row in rows
        if row["dimension_type"] == "molecule_desc" and row["dimension_value_norm"] == "CARTEOLOL"
    )
    dimension_types = {row["dimension_type"] for row in rows}

    assert strength["product_code"] == "P1"
    assert strength["raw_value_history"] == {"2025-01": 125.0}
    assert molecule["product_code"] == "P1"
    assert molecule["dimension_value"] == "CARTEOLOL"
    assert molecule["raw_value_history"] == {"2025-01": 125.0}
    assert "molecule_desc" in dimension_types
    assert "pack" not in dimension_types


def test_build_filter_dimension_rows_collapses_iqvia_brand_display_variants() -> None:
    frame = pd.DataFrame(
        [
            {
                "source": "iqvia_nsa",
                "measure": "sales",
                "atc4_code": "D03A9",
                "brand_key": "큐립",
                "brand_name": "큐립",
                "product_code": "CULIP",
                "period_yyyymm": "2025-01",
                "raw_value": 10.0,
                "company": "Acme",
                "molecule_type": "Single",
                "strength": "1MG",
                "nhi_type": "급여",
            },
            {
                "source": "iqvia_nsa",
                "measure": "sales",
                "atc4_code": "D03A9",
                "brand_key": "큐립",
                "brand_name": "큐립정",
                "product_code": "CULIP",
                "period_yyyymm": "2025-02",
                "raw_value": 20.0,
                "company": "Acme",
                "molecule_type": "Single",
                "strength": "1MG",
                "nhi_type": "급여",
            },
        ]
    )

    rows = sidecar.build_filter_dimension_rows("iqvia_nsa", "sales", frame)
    mfr_rows = [
        row
        for row in rows
        if row["dimension_type"] == "mfr" and row["dimension_value_norm"] == "Acme"
    ]

    assert len(mfr_rows) == 1
    assert mfr_rows[0]["raw_value_history"] == {"2025-01": 10.0, "2025-02": 20.0}


def test_latest_market_period_rejects_stale_ubist_dimension_sidecar() -> None:
    rows = [
        {
            "dimension_type": dimension_type,
            "raw_value_history": {"2026-04": 100.0},
        }
        for dimension_type in (
            "seller",
            "molecule",
            "molecule_strength",
            "form",
            "route",
            "reimbursement",
        )
    ]

    with pytest.raises(RuntimeError, match="2026-05"):
        sidecar.validate_filter_dimension_period_coverage(
            "ubist",
            rows,
            expected_period="2026-05",
        )


def test_latest_market_period_rejects_sidecar_ahead_of_general_mart() -> None:
    rows = [
        {
            "dimension_type": dimension_type,
            "raw_value_history": {"2026-05": 100.0, "2026-06": 100.0},
        }
        for dimension_type in (
            "seller",
            "molecule",
            "molecule_strength",
            "form",
            "route",
            "reimbursement",
        )
    ]

    with pytest.raises(RuntimeError, match="latest period mismatch"):
        sidecar.validate_filter_dimension_period_coverage(
            "ubist",
            rows,
            expected_period="2026-05",
        )


def test_latest_market_period_requires_complete_ubist_dimension_sums() -> None:
    frame = pd.DataFrame(
        [
            {
                "atc4_code": "A10N1",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "product_code": "P1",
                "period_yyyymm": "2026-05",
                "raw_value": 40.0,
                "company": "Seller A",
                "ubist_molecule_raw": "Molecule A",
                "ubist_molecule_strength": "10mg",
                "ubist_form": "Tablet",
                "ubist_route": "Oral",
                "ubist_reimbursement": "Covered",
            },
            {
                "atc4_code": "A10N1",
                "brand_key": "brand-b",
                "brand_name": "Brand B",
                "product_code": "P2",
                "period_yyyymm": "2026-05",
                "raw_value": 60.0,
                "company": "Seller B",
                "ubist_molecule_raw": "Molecule B",
                "ubist_molecule_strength": "20mg",
                "ubist_form": "Capsule",
                "ubist_route": "Oral",
                "ubist_reimbursement": "Covered",
            },
        ]
    )
    rows = sidecar.build_filter_dimension_rows("ubist", "sales", frame)

    coverage = sidecar.validate_filter_dimension_period_coverage(
        "ubist",
        rows,
        expected_period="2026-05",
        expected_total=100.0,
    )

    assert set(coverage) >= {
        "seller",
        "molecule",
        "molecule_strength",
        "form",
        "route",
        "reimbursement",
    }
    assert all(item["period_sum"] == 100.0 for item in coverage.values())


def test_latest_market_period_rejects_incomplete_dimension_total() -> None:
    dimensions = (
        "seller",
        "molecule",
        "molecule_strength",
        "form",
        "route",
        "reimbursement",
    )
    rows = [
        {
            "dimension_type": dimension_type,
            "raw_value_history": {"2026-05": 99.0 if dimension_type == "form" else 100.0},
        }
        for dimension_type in dimensions
    ]

    with pytest.raises(RuntimeError, match="total mismatch"):
        sidecar.validate_filter_dimension_period_coverage(
            "ubist",
            rows,
            expected_period="2026-05",
            expected_total=100.0,
        )


def test_guard_dimension_stage_rejects_operating_schemas() -> None:
    for target_db in ("jw_mart", "jw_mart_test_stage2", "jw_mart_d1_stage_20260625_173115", "scratch"):
        try:
            sidecar.guard_dimension_stage_target(target_db)
        except ValueError as exc:
            assert target_db in str(exc)
        else:
            raise AssertionError(f"expected {target_db} to be rejected")


def test_guard_dimension_stage_accepts_new_dimension_stage_schema() -> None:
    sidecar.guard_dimension_stage_target("jw_mart_dim_stage_20260626_010203")


def test_filter_dimension_ddl_preserves_long_values_and_indexes_hash() -> None:
    ddl = filter_dimension_table_ddl()

    assert "dimension_value TEXT NOT NULL" in ddl
    assert "dimension_value_norm TEXT NOT NULL" in ddl
    assert "dimension_value_hash CHAR(64) NOT NULL" in ddl
    assert "idx_filter_lookup (source, measure, dimension_type, dimension_value_hash" in ddl
