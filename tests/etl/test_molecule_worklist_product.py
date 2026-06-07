"""Molecule worklist propagation to SKU product catalogs."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl import apply_molecule_worklist


def test_product_worklist_update_preserves_raw_molecule_and_sku_dimensions():
    products = pd.DataFrame(
        [
            {
                "product_id": "sp_006_00001_001",
                "brand_id": "sb_006_00001",
                "molecule": "atorvastatin calcium trihydrate (as atorvastatin), ezetimibe",
                "dosage_form": "Oral Solid Ordinary Film-Coated Tablets",
                "strength_pack": "10/10mg",
                "nhi_type": "NHI",
            },
            {
                "product_id": "sp_006_00002_001",
                "brand_id": "sb_006_00002",
                "molecule": "rosuvastatin calcium (as rosuvastatin)",
                "dosage_form": "Oral Solid Ordinary Tablets",
                "strength_pack": "10mg",
                "nhi_type": "NON-NHI",
            },
        ]
    )
    worklist_rows = [
        {
            "level": "ml",
            "brand_id": "sb_006_00001",
            "action": "UPDATE",
            "target_value": "Statin/EZE",
        }
    ]
    brands = pd.DataFrame(
        [
            {"brand_id": "sb_006_00001", "molecule": "Statin/EZE", "dosage_form": None},
            {"brand_id": "sb_006_00002", "molecule": "Statin", "dosage_form": None},
        ]
    )

    updated, update_count, setnull_count = apply_molecule_worklist.apply_product_level(
        products,
        worklist_rows,
        level="ml",
        brand_df=brands,
    )

    first = updated.loc[updated["brand_id"] == "sb_006_00001"].iloc[0]
    second = updated.loc[updated["brand_id"] == "sb_006_00002"].iloc[0]
    assert update_count == 1
    assert setnull_count == 0
    assert first["molecule"] == "Statin/EZE"
    assert first["molecule_raw"] == "atorvastatin calcium trihydrate (as atorvastatin), ezetimibe"
    assert first["dosage_form_raw"] == "Oral Solid Ordinary Film-Coated Tablets"
    assert first["strength_pack"] == "10/10mg"
    assert first["nhi_type"] == "NHI"
    assert second["molecule"] == "Statin"
    assert second["molecule_raw"] == "rosuvastatin calcium (as rosuvastatin)"
    assert second["dosage_form_raw"] == "Oral Solid Ordinary Tablets"
