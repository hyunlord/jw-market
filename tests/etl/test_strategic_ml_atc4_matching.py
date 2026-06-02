"""Strategic ML matching must respect MI Master ATC4 market definitions."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts"))

from pipeline.scripts import prototype_09_master_brand_consolidation_to_parquet as p09
from pipeline.scripts import prototype_20_strategic_brand_to_parquet as p20
from pipeline.scripts.etl.layer3_compute_strategic_ml_v3 import build_ml_rows


def _ml_row(ml_id: str, *, atc4_codes: list[str] | None = None, data_source: str = "iqvia") -> pd.Series:
    return pd.Series(
        {
            "ml_id": ml_id,
            "name": f"{ml_id} market",
            "data_source": data_source,
            "atc_codes_json": json.dumps(atc4_codes or [], ensure_ascii=False),
            "analyze_class": True,
            "analyze_molecule": False,
            "analyze_dosage_form": False,
            "analyze_strength_pack": False,
            "analyze_nhi_type": True,
            "analyze_ox_gx": False,
            "analyze_fish_oil": False,
        }
    )


def _catalog_row(
    *,
    brand_id: str,
    name: str,
    ml_id: str,
    allowed_atc4: list[str] | None,
    class_label: str = "Class A",
    nhi_type: str = "NHI",
    is_class_excluded: bool = False,
) -> dict[str, object]:
    return {
        "brand_id": brand_id,
        "name": name,
        "merge_name": name,
        "ml_id": ml_id,
        "brand_key": name,
        "general_brand_key": name,
        "canonical_name": name,
        "is_jw": False,
        "is_target": False,
        "class": class_label,
        "class_1": class_label,
        "class_2": None,
        "molecule": None,
        "dosage_form": None,
        "strength_pack": None,
        "nhi_type": nhi_type,
        "ox_gx": None,
        "fish_oil": None,
        "allowed_atc4_codes_json": json.dumps(allowed_atc4, ensure_ascii=False) if allowed_atc4 is not None else None,
        "is_class_excluded": is_class_excluded,
    }


def _general_row(
    *,
    brand_key: str,
    atc4_code: str,
    raw_value: float,
    source: str = "iqvia_nsa",
    measure: str = "sales",
    class_label: str = "Class A",
    nhi_type: str = "NHI",
) -> dict[str, object]:
    return {
        "brand_key": brand_key,
        "brand_name": brand_key,
        "atc4_code": atc4_code,
        "atc4_desc": atc4_code,
        "source": source,
        "measure": measure,
        "unit_label": "KRW",
        "metric_history": {"2025-Q4": {"raw_value": raw_value}},
        "extended_metric_history": {},
        "channel_data": {},
        "specialty_data": {},
        "dimension_data": {
            "nhi_type": {
                nhi_type: {
                    "2025-Q4": {"raw_value": raw_value},
                }
            },
            "class": {
                class_label: {
                    "2025-Q4": {"raw_value": raw_value},
                }
            },
        },
        "dimension_channel_data": {},
        "by_dimension": {
            "class": class_label,
            "nhi_type": nhi_type,
            "company": "TestCo",
        },
        "raw_value_history": {"2025-Q4": raw_value},
        "payload": {},
    }


def _with_required_iqvia_measures(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Add minimal non-sales rows so strategic completeness guards stay active."""

    result = list(rows)
    for measure in ("unit", "dosage_unit", "counting_unit"):
        for seed in rows:
            clone = deepcopy(seed)
            clone["measure"] = measure
            clone["raw_value_history"] = {"2025-Q4": 1.0}
            clone["metric_history"] = {"2025-Q4": {"raw_value": 1.0}}
            result.append(clone)
    return result


def _with_required_ubist_measures(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result = list(rows)
    for seed in rows:
        clone = deepcopy(seed)
        clone["measure"] = "volume"
        clone["raw_value_history"] = {"2025-Q4": 1.0}
        clone["metric_history"] = {"2025-Q4": {"raw_value": 1.0}}
        result.append(clone)
    return result


def _market_for(market_rows: list[dict[str, object]], measure: str) -> dict[str, object]:
    return next(row for row in market_rows if row["measure"] == measure)


def test_build_ml_rows_filters_multi_atc4_brand_to_mi_master_allowed_codes():
    """젤잔즈 in 악템라 should keep L04D0 and exclude A07E9."""

    catalog = pd.DataFrame(
        [
            _catalog_row(
                brand_id="sb_011_00022",
                name="젤잔즈",
                ml_id="ml_011",
                allowed_atc4=["L04D0"],
            )
        ]
    )
    general_rows = _with_required_iqvia_measures([
        _general_row(brand_key="젤잔즈", atc4_code="A07E9", raw_value=590_947_536),
        _general_row(brand_key="젤잔즈", atc4_code="L04D0", raw_value=2_034_020_862),
    ])

    brand_rows, market_rows = build_ml_rows(_ml_row("ml_011", atc4_codes=["L04D0"]), catalog, general_rows)
    sales_rows = [row for row in brand_rows if row["measure"] == "sales"]
    sales_market = _market_for(market_rows, "sales")

    assert len(sales_rows) == 1
    assert sales_rows[0]["raw_value_history"]["2025-Q4"] == 2_034_020_862
    assert sales_market["market_size_series"]["2025-Q4"] == 2_034_020_862


def test_build_ml_rows_collapses_allowed_multi_atc4_rows_by_brand_id_and_merges_dimensions():
    """수프렙미니 should sum A06B1 + A06B2 after ATC4 filtering."""

    catalog = pd.DataFrame(
        [
            _catalog_row(
                brand_id="sb_002_00017",
                name="수프렙미니",
                ml_id="ml_002",
                allowed_atc4=["A06B1", "A06B2"],
            )
        ]
    )
    general_rows = _with_required_iqvia_measures([
        _general_row(brand_key="수프렙미니", atc4_code="A06B1", raw_value=100_000_000, nhi_type="NHI"),
        _general_row(brand_key="수프렙미니", atc4_code="A06B2", raw_value=197_874_000, nhi_type="NON-NHI"),
    ])

    brand_rows, market_rows = build_ml_rows(_ml_row("ml_002", atc4_codes=["A06B1", "A06B2"]), catalog, general_rows)
    sales_rows = [row for row in brand_rows if row["measure"] == "sales"]
    sales_market = _market_for(market_rows, "sales")

    assert len(sales_rows) == 1
    row = sales_rows[0]
    assert row["raw_value_history"]["2025-Q4"] == 297_874_000
    assert sales_market["market_size_series"]["2025-Q4"] == 297_874_000
    assert row["dimension_data"]["nhi_type"]["NHI"]["2025-Q4"]["raw_value"] == 100_000_000
    assert row["dimension_data"]["nhi_type"]["NON-NHI"]["2025-Q4"]["raw_value"] == 197_874_000


def test_build_ml_rows_matches_catalog_atc4_to_ubist_bracket_code_alias():
    """엔커버 should match MI Master V06D0 to UBIST general V6D."""

    catalog = pd.DataFrame(
        [
            _catalog_row(
                brand_id="sb_015_00001",
                name="엔커버",
                ml_id="ml_015",
                allowed_atc4=["V06D0"],
            )
        ]
    )
    general_rows = _with_required_ubist_measures([
        _general_row(brand_key="엔커버", atc4_code="V6D", raw_value=123_000_000, source="ubist"),
    ])

    brand_rows, market_rows = build_ml_rows(_ml_row("ml_015", atc4_codes=["V06D0"], data_source="ubist"), catalog, general_rows)
    sales_rows = [row for row in brand_rows if row["measure"] == "sales"]
    sales_market = _market_for(market_rows, "sales")

    assert len(sales_rows) == 1
    assert sales_rows[0]["raw_value_history"]["2025-Q4"] == 123_000_000
    assert sales_market["market_size_series"]["2025-Q4"] == 123_000_000
    assert sales_rows[0]["overlay_data"]["allowed_atc4_codes"] == ["V06D0"]
    assert "V6D" in sales_rows[0]["overlay_data"]["allowed_atc4_aliases"]


def test_class_only_excluded_rows_remain_in_market_total_but_not_class_level():
    catalog = pd.DataFrame(
        [
            _catalog_row(
                brand_id="sb_016_00056",
                name="염화칼륨 중외",
                ml_id="ml_016",
                allowed_atc4=["K01A9"],
                class_label="기타(제외)",
                is_class_excluded=True,
            ),
            _catalog_row(
                brand_id="sb_016_00057",
                name="정상브랜드",
                ml_id="ml_016",
                allowed_atc4=["K01A9"],
                class_label="Class A",
            ),
        ]
    )
    general_rows = _with_required_iqvia_measures([
        _general_row(brand_key="염화칼륨 중외", atc4_code="K01A9", raw_value=8_235_075, class_label="기타(제외)"),
        _general_row(brand_key="정상브랜드", atc4_code="K01A9", raw_value=10_000_000, class_label="Class A"),
    ])

    _brand_rows, market_rows = build_ml_rows(_ml_row("ml_016", atc4_codes=["K01A9"]), catalog, general_rows)
    sales_market = _market_for(market_rows, "sales")

    assert sales_market["market_size_series"]["2025-Q4"] == 18_235_075
    class_level = sales_market["analysis_levels"]["class"]
    assert class_level == {"Class A": {"2025-Q4": 10_000_000.0}}


def test_class_exclusion_cells_are_not_strict_row_exclusions():
    headers = ["PRODUCT NAME KOR", "Class", "Remark"]

    row_excluded, class_excluded = p20.classify_exclusion_cells(headers, ["젤잔즈", "기타(제외)", None])
    assert row_excluded is False
    assert class_excluded is True

    assert p09.is_excluded_row(["젤잔즈", "기타(제외)", None], headers=headers) is False
    assert p09.is_excluded_row(["젤잔즈", "JAK", "제외"], headers=headers) is True
