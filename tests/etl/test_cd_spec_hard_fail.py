from __future__ import annotations

from dataclasses import replace

import pytest

from pipeline.etl.io.catalog.dim.market_competitive_dynamics_specs import (
    _CD_BUSINESS_SPECS,
    build_cd_specs,
)
from pipeline.etl.mi_master_registry import default_mi_master_registry


def _registry_with_cd(topology: dict[str, object]):
    registry = default_mi_master_registry()
    return replace(registry, cd_specs=(*registry.cd_specs, topology))


def test_existing_nineteen_cd_specs_have_explicit_matching_identity() -> None:
    specs = build_cd_specs()

    assert len(specs) == 19
    assert {spec["competitive_dynamics_id"] for spec in specs} == {
        f"cd_{index:03d}" for index in range(1, 20)
    }


def test_missing_cd_business_spec_raises_value_error() -> None:
    registry = _registry_with_cd(
        {
            "cd_id": "cd_020",
            "name": "신규시장",
            "ml_id": "ml_017",
            "cd_filter_id": "cdf_020",
            "strategic_market_id": "strategy_017",
            "column_ids": (23,),
        }
    )

    with pytest.raises(ValueError, match=r"cd_020.*missing_explicit_spec"):
        build_cd_specs(registry)


def test_matching_new_cd_business_spec_is_accepted() -> None:
    topology = {
        "cd_id": "cd_020",
        "name": "신규시장",
        "ml_id": "ml_017",
        "cd_filter_id": "cdf_020",
        "strategic_market_id": "strategy_017",
        "column_ids": (23,),
    }
    business = {
        "competitive_dynamics_id": "cd_020",
        "strategic_market_id": "strategy_017",
        "product_name_kor": "신규시장",
        "col_in_master_excel": "W",
        "column_ids": (23,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    }

    specs = build_cd_specs(
        _registry_with_cd(topology),
        business_specs=(*_CD_BUSINESS_SPECS, business),
    )

    assert specs[-1] == business


def test_cd_business_spec_identity_mismatch_raises_value_error() -> None:
    registry = default_mi_master_registry()
    moved = tuple(
        {
            **topology,
            "strategic_market_id": "strategy_005",
            "name": "신규시장",
        }
        if topology["cd_id"] == "cd_006"
        else topology
        for topology in registry.cd_specs
    )

    with pytest.raises(ValueError) as caught:
        build_cd_specs(replace(registry, cd_specs=moved))

    message = str(caught.value)
    assert "cd_006" in message
    assert "strategic_market_id" in message
    assert "product_name_kor" in message
    assert "strategy_006" in message
    assert "strategy_005" in message


def test_product_identity_rejects_reversed_product_order() -> None:
    registry = default_mi_master_registry()
    reversed_name = tuple(
        {**topology, "name": "리바로젯/리바로"}
        if topology["cd_id"] == "cd_006"
        else topology
        for topology in registry.cd_specs
    )

    with pytest.raises(ValueError, match=r"cd_006.*product_name_kor"):
        build_cd_specs(replace(registry, cd_specs=reversed_name))
