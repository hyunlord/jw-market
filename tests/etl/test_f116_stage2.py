from __future__ import annotations

import pandas as pd

from pipeline.etl.io.catalog.postfix.canonical import CanonicalBrand, _canonical_row
from pipeline.etl.io.mart.strategic_dimensions import _build_channel_context
from pipeline.etl.io.mart.strategic_dimension_apply import enhance_strategic_dimensions
from pipeline.etl.io.mart.ubist_channel_mapping import (
    aggregate_specialty_labels,
)


def test_strategic_specialty_context_drops_catalog_aggregate_parent() -> None:
    aggregate_parent = next(iter(aggregate_specialty_labels()))
    rows = [
        {
            "product_code": "P1",
            "channel": "의원",
            "specialty": aggregate_parent,
            "period_yyyymm": "2025-01",
            "raw_sales": 100,
            "raw_volume": 10,
        },
        {
            "product_code": "P1",
            "channel": "CL",
            "specialty": "Endo",
            "period_yyyymm": "2025-01",
            "raw_sales": 10,
            "raw_volume": 1,
        },
    ]

    context = _build_channel_context(pd.DataFrame(rows), {"P1": {"molecule": "M1"}})
    specialty_history = context["code_specialty_history"]["sales"]["P1"]["molecule"]["M1"]

    assert all(aggregate_parent not in label for label in specialty_history)
    assert len(specialty_history) == 1
    assert sum(periods["2025-01"]["raw_value"] for periods in specialty_history.values()) == 10
    channel_history = context["code_channel_history"]["sales"]["P1"]["molecule"]["M1"]
    assert sum(periods["2025-01"]["raw_value"] for periods in channel_history.values()) == 10


def test_strategic_specialty_context_preserves_unclassified_specialty_value() -> None:
    rows = [
        {
            "product_code": "P1",
            "channel": "TH",
            "specialty": "Unknown",
            "period_yyyymm": "2025-01",
            "raw_sales": 25,
            "raw_volume": 2,
        }
    ]

    context = _build_channel_context(pd.DataFrame(rows), {"P1": {"molecule": "M1"}})
    specialty_history = context["code_specialty_history"]["sales"]["P1"]["molecule"]["M1"]
    channel_history = context["code_channel_history"]["sales"]["P1"]["molecule"]["M1"]

    assert specialty_history == {
        "종합병원 분리되지 않은 진료과": {"2025-01": {"raw_value": 25.0}}
    }
    assert sum(periods["2025-01"]["raw_value"] for periods in specialty_history.values()) == sum(
        periods["2025-01"]["raw_value"] for periods in channel_history.values()
    )


def test_strategic_specialty_applies_each_product_code_once() -> None:
    row = {
        "brand_id": "B1",
        "source": "ubist",
        "measure": "sales",
        "raw_value_history": {},
        "channel_data": {},
        "dimension_data": {},
        "dimension_channel_data": {},
        "dimension_specialty_data": {},
        "by_dimension": {
            "products": [
                {"product_code": "P1"},
                {"product_code": "P1"},
            ]
        },
    }
    context = {
        "brand_single_dimensions": {},
        "code_dimensions": {"P1": {"molecule": "M1"}},
        "code_channel_history": {"sales": {}},
        "code_specialty_history": {
            "sales": {
                "P1": {
                    "molecule": {
                        "M1": {
                            "의원 내분비": {"2025-01": {"raw_value": 10.0}}
                        }
                    }
                }
            }
        },
    }

    result = enhance_strategic_dimensions(row, context)

    specialty = result["dimension_specialty_data"]["molecule"]["M1"]["의원 내분비"]
    assert specialty["2025-01"]["raw_value"] == 10.0


def test_canonical_row_falls_back_per_field_without_inventing_molecule() -> None:
    table = pd.DataFrame(
        [
            {
                "brand_id": "source-1",
                "name": "GUARDMET",
                "merge_name": "GUARDMET",
                "ml_id": "ml_003",
                "cd_id": "cd_003",
                "class": None,
                "molecule": None,
                "dosage_form": "-",
                "strength_pack": "brand strength",
                "nhi_type": None,
                "제조사": "없음",
            }
        ]
    )
    products = pd.DataFrame(
        [
            {
                "name": "GUARDMET XR",
                "ml_id": "ml_003",
                "cd_id": "cd_003",
                "class": "DPP-4i+MET",
                "molecule": None,
                "dosage_form": "Oral",
                "strength_pack": "product strength",
                "nhi_type": "급여",
                "제조사": "JW중외제약",
            }
        ]
    )

    row = _canonical_row(
        table,
        products,
        CanonicalBrand("가드메트", "ml_003", "cd_003", False, contains=("GUARDMET",)),
        id_col="cd_id",
        row_index=1,
        brand_id_prefix="canonical",
    )

    assert row["class"] == "DPP-4i+MET"
    assert row["dosage_form"] == "Oral"
    assert row["strength_pack"] == "brand strength"
    assert row["nhi_type"] == "급여"
    assert row["제조사"] == "JW중외제약"
    assert row["molecule"] is None
