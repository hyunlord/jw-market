from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pipeline.contracts.dimension_registry import (
    api_dimension_name,
    api_dimension_names,
    canonical_dimension_name,
    dimension_candidates,
    dimension_label,
    dimension_sort_key,
    enabled_dimension_specs,
)
from pipeline.etl.io.catalog.dim.market_landscape_schema import (
    EXPECTED_MARKET_IDS as LANDSCAPE_MARKET_IDS,
)
from pipeline.etl.io.catalog.market.cd_filter_schema import EXPECTED_CD_FILTER_IDS
from pipeline.etl.io.catalog.market.cd_market_schema import EXPECTED_CD_IDS
from pipeline.etl.io.catalog.market.ml_market_schema import (
    EXPECTED_MARKET_IDS,
    EXPECTED_ML_IDS,
)
from pipeline.etl.mi_master_registry import default_mi_master_registry
from pipeline.domain.brand_names import normalize_brand_name
from pipeline.domain.molecules import split_molecule_components
from pipeline.domain.momentum import compute_market_share_momentum
from pipeline.etl.io.mart.brand_key_normalize import (
    normalize_brand_name as legacy_normalize_brand_name,
)
from pipeline.etl.io.mart.molecule_normalize import (
    split_molecule_components as legacy_split_molecule_components,
)
from pipeline.etl.io.mart.momentum import (
    compute_market_share_momentum as legacy_compute_market_share_momentum,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("source", "internal_name", "public_name"),
    (
        ("ubist", "atc3", "atc3"),
        ("ubist", "atc4", "atc4"),
        ("ubist", "seller", "seller"),
        ("ubist", "molecule", "molecule"),
        ("ubist", "molecule_strength", "molecule_strength"),
        ("ubist", "form", "form"),
        ("ubist", "route", "route"),
        ("ubist", "reimbursement", "reimbursement"),
        ("iqvia_nsa", "mfr", "mfr_name_kor"),
        ("iqvia_nsa", "molecule_desc", "molecule_desc"),
        ("iqvia_nsa", "molecule_type", "molecule_type"),
        ("iqvia_nsa", "pack", "pack_desc"),
        ("iqvia_nsa", "strength", "strength"),
        ("iqvia_nsa", "nhi", "nhi_type"),
    ),
)
def test_dimension_public_names_round_trip(
    source: str,
    internal_name: str,
    public_name: str,
) -> None:
    assert api_dimension_name(source, internal_name) == public_name
    assert canonical_dimension_name(source, public_name) == internal_name


@pytest.mark.parametrize(
    ("key", "payload", "expected"),
    (
        ("seller", {"mfr": "JW"}, ("JW",)),
        ("class", {"market_class": "SYKi"}, ("SYKi",)),
        ("mfr_name_kor", {"company_name": "JW"}, ("JW",)),
        ("mfr", {"mfr_name_kor": "JW"}, ("JW",)),
        ("molecule", {"molecule_desc": "FOSTAMATINIB"}, ("FOSTAMATINIB",)),
        ("molecule_strength", {"strength_pack": ["100MG", "150MG"]}, ("100MG", "150MG")),
        ("strength_pack", {"성분용량": "2MG"}, ("2MG",)),
        ("ox_gx", {"oxgx": "Original"}, ("Original",)),
        ("form", {"dosage_form": "Tablet"}, ("Tablet",)),
        ("route", {"투여경로": "Oral"}, ("Oral",)),
        ("reimbursement", {"nhi_type": "급여"}, ("급여",)),
        ("nhi", {"급여구분": "비급여"}, ("비급여",)),
        ("nhi_type", {"nhi": "NHI"}, ("NHI",)),
        ("atc3", {"atc3_code": "C10"}, ("C10",)),
        ("atc4", {"atc4_code": "C10C"}, ("C10C",)),
        ("audit_code", {"audit_code": ["KHPA", "KCPA"]}, ("KHPA", "KCPA")),
        ("fish_oil", {"fish_oil": "EPA/DHA"}, ("EPA/DHA",)),
        ("molecule", {"molecule": "", "molecule_desc": "ANAGLIPTIN"}, ("ANAGLIPTIN",)),
        ("seller", {"seller": ["JW", "Competitor"]}, ("JW", "Competitor")),
        ("unknown_axis", {"unknown_axis": "kept"}, ("kept",)),
    ),
)
def test_twenty_dimension_payloads_keep_exact_serialized_candidates(
    key: str,
    payload: dict[str, str | list[str]],
    expected: tuple[str, ...],
) -> None:
    actual = dimension_candidates(payload, key)
    assert json.dumps(actual, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        expected,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_registry_preserves_enabled_order_labels_and_sorting() -> None:
    assert tuple(spec.dimension_type for spec in enabled_dimension_specs("ubist")) == (
        "atc3",
        "atc4",
        "seller",
        "molecule",
        "molecule_strength",
        "form",
        "route",
        "reimbursement",
    )
    assert tuple(spec.dimension_type for spec in enabled_dimension_specs("iqvia_nsa")) == (
        "mfr",
        "molecule_desc",
        "molecule_type",
        "pack",
        "strength",
        "nhi",
    )
    assert dimension_label("mfr_name_kor") == "MFR NAME KOR"
    assert dimension_label("unknown_axis") == "unknown_axis"
    assert dimension_sort_key("class") < dimension_sort_key("audit_code")
    assert dimension_sort_key("unknown_axis") > dimension_sort_key("audit_code")
    assert api_dimension_names("iqvia_nsa", include_shared=True) == {
        "atc4": "atc4",
        "mfr_name_kor": "mfr",
        "molecule_desc": "molecule_desc",
        "molecule_type": "molecule_type",
        "pack_desc": "pack",
        "strength": "strength",
        "nhi_type": "nhi",
    }


def test_api_layer_does_not_import_etl_mart_implementation_modules() -> None:
    violations: list[str] = []
    api_root = ROOT / "pipeline" / "scripts" / "api"
    for path in sorted(api_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pipeline.etl.io.mart"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("pipeline.etl.io.mart"):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}")
    assert violations == []


def test_market_validation_ids_keep_exact_existing_order_without_count_literals() -> None:
    registry = default_mi_master_registry()
    assert EXPECTED_ML_IDS == tuple(registry.analyze_matrix)
    assert EXPECTED_MARKET_IDS == tuple(
        market.strategic_market_id for market in registry.market_sheets
    )
    assert LANDSCAPE_MARKET_IDS == EXPECTED_MARKET_IDS
    assert EXPECTED_CD_IDS == tuple(
        str(spec["cd_id"]) for spec in registry.cd_specs
    )
    assert EXPECTED_CD_FILTER_IDS == tuple(
        str(spec["cd_filter_id"]) for spec in registry.cd_specs
    )


def test_legacy_etl_imports_reexport_the_shared_domain_functions() -> None:
    assert legacy_normalize_brand_name is normalize_brand_name
    assert legacy_split_molecule_components is split_molecule_components
    assert legacy_compute_market_share_momentum is compute_market_share_momentum
