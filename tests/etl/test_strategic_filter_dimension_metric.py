from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.etl.io.mart.strategic_filter_dimension_metric import (
    StrategicMetricSourceRow,
    extract_dimension_metric_rows,
    normalize_dimension_value,
)


def test_extracts_single_recode_dimension_at_product_grain() -> None:
    row = StrategicMetricSourceRow(
        market_kind="ml",
        market_id="ml_005",
        brand_id="sb_005_00051",
        brand_key="미케란",
        brand_name="미케란",
        source="ubist",
        measure="sales",
        unit_label="KRW",
        raw_value_history=json.dumps({"2026-04": 31_282_626.06}),
        by_dimension=json.dumps(
            {
                "company": "태준제약",
                "products": [
                    {
                        "product_code": "649900100",
                        "product_name": "미케란 정 5mg",
                        "raw_value_history": {"2026-04": 31_282_626.06},
                    }
                ],
                "strength_pack": "carteolol HCl 5mg [124801ATB]",
                "atc4_code": "S01ED",
            },
            ensure_ascii=False,
        ),
        dimension_data=json.dumps(
            {"strength_pack": {"carteolol HCl 5mg [124801ATB]": {"2026-04": {"raw_value": 31_282_626.06}}}},
            ensure_ascii=False,
        ),
        overlay_data="{}",
        cd_overlay=None,
    )

    extracted = extract_dimension_metric_rows(row, molecule_type_by_product={})
    rows_by_type = {item.dimension_type: item for item in extracted}

    assert rows_by_type["seller"].product_code == "649900100"
    assert rows_by_type["seller"].dimension_value == "태준제약"
    assert rows_by_type["atc4"].product_code == "649900100"
    assert rows_by_type["atc4"].dimension_value == "S01ED"
    assert rows_by_type["atc3"].product_code == "649900100"
    assert rows_by_type["atc3"].dimension_value == "S01E"
    assert rows_by_type["molecule_strength"].product_code == "649900100"
    assert rows_by_type["molecule_strength"].raw_value_history == {"2026-04": 31_282_626.06}


def test_extracts_multi_label_dimension_from_dimension_history_without_overassigning_products() -> None:
    row = StrategicMetricSourceRow(
        market_kind="ml",
        market_id="ml_x",
        brand_id="brand_x",
        brand_key="brand_x",
        brand_name="Brand X",
        source="ubist",
        measure="sales",
        unit_label="KRW",
        raw_value_history=json.dumps({"2026-04": 30.0}),
        by_dimension=json.dumps(
            {
                "products": [
                    {"product_code": "p1", "product_name": "P1", "raw_value_history": {"2026-04": 10.0}},
                    {"product_code": "p2", "product_name": "P2", "raw_value_history": {"2026-04": 20.0}},
                ]
            }
        ),
        dimension_data=json.dumps(
            {
                "dosage_form": {
                    "정제": {"2026-04": {"raw_value": 10.0}},
                    "주사제": {"2026-04": {"raw_value": 20.0}},
                }
            },
            ensure_ascii=False,
        ),
        overlay_data="{}",
        cd_overlay=None,
    )

    extracted = [item for item in extract_dimension_metric_rows(row, molecule_type_by_product={}) if item.dimension_type == "form"]

    assert [item.dimension_value for item in extracted] == ["정제", "주사제"]
    assert all(item.product_code.startswith("__dimension__:form:") for item in extracted)
    assert sum(item.raw_value_history["2026-04"] for item in extracted) == 30.0


def test_iqvia_molecule_type_uses_raw_label_but_strategic_history() -> None:
    row = StrategicMetricSourceRow(
        market_kind="cd",
        market_id="cd_002",
        brand_id="brand_iq",
        brand_key="nexcolon",
        brand_name="넥스콜론",
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        raw_value_history=json.dumps({"2025-Q4": 7.0}),
        by_dimension=json.dumps(
            {
                "products": [
                    {"product_code": "NEXCOLON", "product_name": "넥스콜론", "raw_value_history": {"2025-Q4": 7.0}}
                ],
                "nhi_type": "NON-NHI",
            },
            ensure_ascii=False,
        ),
        dimension_data=json.dumps({"nhi_type": {"NON-NHI": {"2025-Q4": {"raw_value": 7.0}}}}, ensure_ascii=False),
        overlay_data="{}",
        cd_overlay=None,
    )

    extracted = extract_dimension_metric_rows(row, molecule_type_by_product={"NEXCOLON": "SINGLE"})
    molecule_type = [item for item in extracted if item.dimension_type == "molecule_type"]

    assert len(molecule_type) == 1
    assert molecule_type[0].dimension_value == "SINGLE"
    assert molecule_type[0].raw_value_history == {"2025-Q4": 7.0}


def test_iqvia_molecule_desc_uses_raw_molecule_label() -> None:
    row = StrategicMetricSourceRow(
        market_kind="cd",
        market_id="cd_002",
        brand_id="brand_iq",
        brand_key="nexcolon",
        brand_name="넥스콜론",
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        raw_value_history=json.dumps({"2025-Q4": 7.0}),
        by_dimension=json.dumps(
            {
                "products": [
                    {"product_code": "NEXCOLON", "product_name": "넥스콜론", "raw_value_history": {"2025-Q4": 7.0}}
                ],
                "molecule": "CARTEOLOL",
                "nhi_type": "NON-NHI",
            },
            ensure_ascii=False,
        ),
        dimension_data=json.dumps({"molecule": {"CARTEOLOL": {"2025-Q4": {"raw_value": 7.0}}}}, ensure_ascii=False),
        overlay_data="{}",
        cd_overlay=None,
    )

    extracted = extract_dimension_metric_rows(row, molecule_type_by_product={"NEXCOLON": "SINGLE"})
    molecule_desc = [item for item in extracted if item.dimension_type == "molecule_desc"]

    assert len(molecule_desc) == 1
    assert molecule_desc[0].dimension_value == "CARTEOLOL"
    assert molecule_desc[0].raw_value_history == {"2025-Q4": 7.0}


def test_normalize_dimension_value_collapses_spacing_and_case() -> None:
    assert normalize_dimension_value("  Non- NHI\t") == "non- nhi"
