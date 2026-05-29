import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_cause import _dimension_values, _response_levels, _strategic_levels


def test_ml011_uses_distinct_class_axes():
    market = {"ml_id": "ml_011", "analyze_class": True}

    assert _strategic_levels(market, "ml_011")[:2] == ["Class 1", "Class 2"]
    assert _response_levels(market, "ml_011")[:2] == ["Class 1", "Class 2"]


def test_cd014_inherits_ml011_class_split():
    market = {"ml_id": "ml_011", "analyze_class": True}

    assert _strategic_levels(market, "cd_014")[:2] == ["Class 1", "Class 2"]
    assert _response_levels(market, "cd_014")[:2] == ["Class 1", "Class 2"]


def test_class_1_and_class_2_do_not_fallback_to_class():
    row = {
        "__by_dimension": {
            "class": "IL-6",
            "class_1": "Biologics",
            "class_2": "IL-6",
        }
    }

    assert _dimension_values(row, "Class 1") == ["Biologics"]
    assert _dimension_values(row, "Class 2") == ["IL-6"]


def test_missing_split_values_stay_empty_instead_of_fallback():
    row = {"__by_dimension": {"class": "IL-6"}}

    assert _dimension_values(row, "Class 1") == []
    assert _dimension_values(row, "Class 2") == []
