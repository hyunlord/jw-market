from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.internal_adapters import InternalToolAdapterRegistry
from jw_chat_agent_poc.tool_use.market_definition_registry import (
    MarketDefinitionRegistry,
    StaticMarketDefinitionCatalogReader,
)
from jw_chat_agent_poc.tool_use.market_scope_contract import (
    GeneralCompositeUnavailableError,
    InvalidMarketLabelError,
)
from jw_chat_agent_poc.tool_use.v3_execution import V3ShadowToolExecutor
from jw_chat_agent_poc.tool_use.v3_execution_contracts import MarketDefinitionFact
from jw_chat_agent_poc.tool_use.v3_execution_tools import internal_executable_tools
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import fusion_fact_payload
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice, selection_tool_specs


ML_ROWS = (
    {
        "ml_id": "ml_008",
        "name": "리바로하이 리바로브이",
        "data_source": "ubist",
        "atc_codes_json": '["C11A1", "C3A1"]',
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 0,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 1,
        "analyze_fish_oil": 0,
    },
    {
        "ml_id": "ml_011",
        "name": "악템라",
        "data_source": "iqvia",
        "atc_codes_json": '["L01G1", "L04B0", "L04D0", "M01C0"]',
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 0,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 1,
        "analyze_fish_oil": 0,
    },
)
CD_ROWS = (
    {"cd_id": "cd_008", "name": "리바로하이", "ml_id": "ml_008", "cd_filter_id": "cdf_008", "data_source": "ubist"},
    {"cd_id": "cd_009", "name": "리바로브이", "ml_id": "ml_008", "cd_filter_id": "cdf_009", "data_source": "ubist"},
    {"cd_id": "cd_010", "name": "시장 10A", "ml_id": "ml_009", "cd_filter_id": "cdf_010", "data_source": "ubist"},
    {"cd_id": "cd_011", "name": "시장 10B", "ml_id": "ml_009", "cd_filter_id": "cdf_011", "data_source": "ubist"},
    {"cd_id": "cd_012", "name": "시장 12A", "ml_id": "ml_010", "cd_filter_id": "cdf_012", "data_source": "ubist"},
    {"cd_id": "cd_013", "name": "시장 12B", "ml_id": "ml_010", "cd_filter_id": "cdf_013", "data_source": "ubist"},
    {"cd_id": "cd_014", "name": "악템라", "ml_id": "ml_011", "cd_filter_id": "cdf_014", "data_source": "iqvia"},
)
BRAND_ROWS = (
    {
        "brand_id": "brand_actemra",
        "name": "악템라",
        "merge_name": "악템라",
        "canonical_name": "악템라",
        "general_brand_key": "악템라",
        "ml_id": "ml_011",
        "cd_id": "cd_014",
        "is_excluded": 0,
        "is_class_excluded": 0,
        "allowed_atc4_codes_json": '["M01C0"]',
        "class": "IL-6",
        "class_1": "Biologics",
        "class_2": "IL-6",
        "molecule": "TOCILIZUMAB",
        "dosage_form": None,
        "strength_pack": None,
        "nhi_type": None,
        "ox_gx": "Ox",
        "fish_oil": None,
    },
)


class _UnusedMarketLayer:
    def execute_catalog_tool(self, name: str, arguments: object) -> dict[str, object]:
        raise AssertionError(f"definition tool leaked to metric scope backend: {name} {arguments}")


def _registry() -> MarketDefinitionRegistry:
    return MarketDefinitionRegistry(
        StaticMarketDefinitionCatalogReader(
            market_landscape_rows=ML_ROWS,
            competitive_dynamics_rows=CD_ROWS,
            strategic_brand_rows=BRAND_ROWS,
            atc4_rows=({"atc4_code": "M01C0", "atc4_desc": "Specific antirheumatic agents"},),
        )
    )


def test_catalog_adds_one_real_definition_tool_with_rationale_boundary() -> None:
    records = {record.name: record for record in TOOL_DESCRIPTION_CATALOG}

    assert len(records) == 35
    definition = records["market.get_definition"]
    assert definition.selection_enabled is False
    assert definition.examples
    assert definition.not_for
    assert any("선정 사유" in item for item in definition.does_not_return)
    assert "사유 질문" in definition.description
    assert any("왜" in example and "미기록" in example for example in definition.examples)
    assert "market.get_definition" in {spec.name for spec in selection_tool_specs()}


def test_general_view_definition_is_atc4_only() -> None:
    result = _registry().get_definition({"view": "general", "atc4": "M01C0"})

    assert result["view_category"] == "일반뷰"
    assert result["view_name"] == "general_atc4"
    assert result["market_identifier"] == "M01C0"
    assert result["definition_statements"] == [
        "일반뷰 시장은 ATC4 코드 M01C0 하나를 기준으로 정의됩니다."
    ]
    assert result["selection_rationale"]["available"] is False


def test_definition_honors_structured_general_and_strategic_scopes() -> None:
    general = _registry().get_definition(
        {"scope": {"kind": "general_atc4", "atc4": ["M01C0"]}}
    )
    strategic = _registry().get_definition(
        {"scope": {"kind": "strategic", "market_id": "ml_011"}}
    )

    assert general["view_name"] == "general_atc4"
    assert general["market_identifier"] == "M01C0"
    assert strategic["view_name"] == "market_landscape"
    assert strategic["market_identifier"] == "ml_011"


def test_definition_rejects_composite_and_conflicting_view_axes() -> None:
    with pytest.raises(GeneralCompositeUnavailableError):
        _registry().get_definition(
            {"scope": {"kind": "general_composite", "atc4": ["M01C0", "L04B0"]}}
        )

    with pytest.raises(InvalidMarketLabelError):
        _registry().get_definition(
            {
                "view": "competitive_dynamics",
                "market_id": "cd_014",
                "atc4": "M01C0",
            }
        )


def test_definition_without_identifier_returns_only_recorded_view_contract() -> None:
    result = _registry().get_definition({})

    assert result["view_category"] == "시장 정의 체계"
    assert result["view_name"] == "view_contract"
    assert result["selection_rationale"]["available"] is False
    assert result["definition_statements"] == [
        "일반뷰는 ATC4 코드 하나를 기준으로 정의됩니다.",
        "market_landscape와 competitive_dynamics는 모두 전략뷰입니다.",
        "competitive_dynamics는 market_landscape에서 범위를 좁힌 전략뷰입니다.",
    ]


def test_market_landscape_and_competitive_dynamics_are_both_strategic() -> None:
    market_landscape = _registry().get_definition(
        {"market_id": "ml_011", "view": "market_landscape", "brand": "악템라"}
    )
    competitive = _registry().get_definition(
        {"market_id": "cd_014", "view": "competitive_dynamics", "brand": "악템라"}
    )

    assert market_landscape["view_category"] == "전략뷰"
    assert competitive["view_category"] == "전략뷰"
    assert competitive["parent_market_identifier"] == "ml_011"
    assert competitive["narrowing_rule"]["available"] is False
    assert competitive["narrowing_rule"]["reference_id"] == "cdf_014"
    assert "조건 본문은 현재 런타임 카탈로그에서 조회되지 않습니다." in competitive["narrowing_rule"]["message"]


def test_definition_statements_use_public_market_names_instead_of_internal_ids() -> None:
    landscape = _registry().get_definition(
        {"market_id": "ml_008", "view": "market_landscape"}
    )
    competitive = _registry().get_definition(
        {"market_id": "cd_014", "view": "competitive_dynamics"}
    )

    landscape_text = "\n".join(landscape["definition_statements"])
    competitive_text = "\n".join(competitive["definition_statements"])
    assert "리바로하이 리바로브이" in landscape_text
    assert "리바로하이, 리바로브이" in landscape_text
    assert "악템라" in competitive_text
    assert "ml_" not in landscape_text
    assert "cd_" not in landscape_text
    assert "ml_" not in competitive_text
    assert "cd_" not in competitive_text
    assert "—" not in landscape_text
    assert "—" not in competitive_text


def test_definition_without_public_market_name_fails_instead_of_using_placeholder() -> None:
    reader = StaticMarketDefinitionCatalogReader(
        market_landscape_rows=({**ML_ROWS[0], "name": None},),
        competitive_dynamics_rows=(),
        strategic_brand_rows=(),
        atc4_rows=(),
    )

    with pytest.raises(LookupError, match="공개 시장명"):
        MarketDefinitionRegistry(reader).get_definition(
            {"market_id": "ml_008", "view": "market_landscape"}
        )


def test_definition_is_projected_to_user_language_without_internal_field_names() -> None:
    result = _registry().get_definition(
        {"market_id": "ml_011", "view": "market_landscape", "brand": "악템라"}
    )
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert result["market_identifier"] == "ml_011"
    assert result["analysis_dimensions"] == ["Class", "성분", "오리지널/제네릭"]
    assert result["brand_inclusion"]["included"] is True
    assert {item["label"] for item in result["brand_inclusion"]["matched_conditions"]} >= {
        "ATC4",
        "Class 1",
        "Class 2",
        "성분",
    }
    assert result["class_structure"]["display_axis"] == "Class 2"
    assert result["class_structure"]["class_1_values"] == ["Biologics"]
    assert result["class_structure"]["class_2_values"] == ["IL-6"]
    assert "악템라는 ATC4 M01C0, Class IL-6, Class 1 Biologics, Class 2 IL-6, 성분 TOCILIZUMAB, 오리지널/제네릭 Ox 조건으로 포함됩니다." in result["definition_statements"]
    assert "Class 1 구성은 Biologics이고 Class 2 구성은 IL-6입니다." in result["definition_statements"]
    assert "시각화 표시 축은 Class 2입니다." in result["definition_statements"]
    for forbidden in ("cd_filter", "analyze_class", "dosage_form_recode"):
        assert forbidden not in encoded


def test_ml_to_cd_splits_are_read_from_catalog_rows() -> None:
    registry = _registry()

    assert registry.competitive_market_ids("ml_008") == ("cd_008", "cd_009")
    assert registry.competitive_market_ids("ml_009") == ("cd_010", "cd_011")
    assert registry.competitive_market_ids("ml_010") == ("cd_012", "cd_013")


def test_competitive_view_accepts_parent_market_id_for_split_definition() -> None:
    result = _registry().get_definition(
        {"market_id": "ml_008", "view": "competitive_dynamics"}
    )

    assert result["view_category"] == "전략뷰"
    assert result["view_name"] == "competitive_dynamics"
    assert result["parent_market_identifier"] == "ml_008"
    assert result["competitive_market_identifiers"] == ["cd_008", "cd_009"]
    assert result["selection_rationale"]["available"] is False


def test_internal_adapter_dispatches_definition_before_market_scope_backend() -> None:
    adapters = InternalToolAdapterRegistry(
        market_layer=_UnusedMarketLayer(),
        definition_registry=_registry(),
    )

    result = adapters.execute(
        "market.get_definition",
        {"market_id": "ml_011", "view": "market_landscape", "brand": "악템라"},
    )

    assert result["market_identifier"] == "ml_011"


def test_definition_execution_becomes_definition_fact_with_existing_id_format() -> None:
    adapters = InternalToolAdapterRegistry(
        market_layer=_UnusedMarketLayer(),
        definition_registry=_registry(),
    )
    executor = V3ShadowToolExecutor(tools=internal_executable_tools(adapters))

    bundle = executor.execute(
        (
            MultiToolChoice(
                "market.get_definition",
                {"market_id": "ml_011", "view": "market_landscape", "brand": "악템라"},
            ),
        )
    )

    assert bundle.status == "complete"
    assert len(bundle.facts) == 1
    fact = bundle.facts[0]
    assert isinstance(fact, MarketDefinitionFact)
    assert fact.evidence_id.startswith("v3-shadow:market.get_definition:")
    assert len(fact.evidence_id.rsplit(":", 1)[-1]) == 16
    assert fact.market_id == "ml_011"
    assert fact.view == "market_landscape"
    assert fact.missing_required_fields == ()
    prompt_payload = fusion_fact_payload(fact)
    assert prompt_payload["raw_result"] == {
        "selection_rationale": {
            "available": False,
            "message": "시장 선정 사유와 의사결정 배경은 현재 카탈로그에 기록돼 있지 않습니다.",
        }
    }


def test_selection_rationale_is_explicitly_unavailable_not_inferred() -> None:
    result = _registry().get_definition(
        {"market_id": "ml_011", "view": "market_landscape"}
    )

    assert result["selection_rationale"] == {
        "available": False,
        "message": "시장 선정 사유와 의사결정 배경은 현재 카탈로그에 기록돼 있지 않습니다.",
    }
