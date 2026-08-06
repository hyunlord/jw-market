from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jw_chat_agent_poc.tool_use.market_definition_catalog import (
    CatalogRow,
    MariaDbMarketDefinitionCatalogReader,
    MarketDefinitionCatalogReader,
    StaticMarketDefinitionCatalogReader,
)
from jw_chat_agent_poc.tool_use.market_definition_input import definition_axes
from jw_chat_agent_poc.tool_use.market_definition_projection import (
    analysis_dimensions as _analysis_dimensions,
    analysis_statement as _analysis_statement,
    brand_inclusion as _brand_inclusion,
    class_structure as _class_structure,
    class_structure_statements as _class_structure_statements,
    inclusion_statement as _inclusion_statement,
    json_strings as _json_strings,
    source_label as _source_label,
    text as _text,
    unavailable_rationale as _unavailable_rationale,
)


class MarketDefinitionRegistry:
    def __init__(self, reader: MarketDefinitionCatalogReader) -> None:
        self._reader = reader

    def get_definition(self, arguments: Mapping[str, object]) -> dict[str, Any]:
        view, atc4, market_id = definition_axes(arguments)
        brand = _text(arguments.get("brand"))
        if view == "general":
            return self._general_definition(atc4)
        if view == "competitive_dynamics":
            if market_id.startswith("ml_"):
                return self._competitive_children_definition(market_id)
            return self._competitive_definition(market_id, brand)
        if view == "market_landscape":
            return self._landscape_definition(market_id, brand)
        if brand:
            matches = self._reader.strategic_brands(brand=brand)
            market_ids = sorted({_text(row.get("ml_id")) for row in matches if row.get("ml_id")})
            if len(market_ids) != 1:
                raise LookupError(
                    f"브랜드 {brand}의 전략시장 정의를 하나로 확정할 수 없습니다. 후보 수: {len(market_ids)}"
                )
            return self._landscape_definition(market_ids[0], brand)
        return self._view_contract()

    def competitive_market_ids(self, parent_market_id: str) -> tuple[str, ...]:
        return tuple(
            _text(row.get("cd_id"))
            for row in self._reader.competitive_markets(parent_market_id)
        )

    def _view_contract(self) -> dict[str, Any]:
        return {
            "definition_type": "market_definition",
            "view_category": "시장 정의 체계",
            "view_name": "view_contract",
            "market_identifier": "view_contract",
            "definition_statements": [
                "일반뷰는 ATC4 코드 하나를 기준으로 정의됩니다.",
                "market_landscape와 competitive_dynamics는 모두 전략뷰입니다.",
                "competitive_dynamics는 market_landscape에서 범위를 좁힌 전략뷰입니다.",
            ],
            "selection_rationale": _unavailable_rationale(),
        }

    def _general_definition(self, atc4: str) -> dict[str, Any]:
        row = self._reader.atc4(atc4) or {}
        result: dict[str, Any] = {
            "definition_type": "market_definition",
            "view_category": "일반뷰",
            "view_name": "general_atc4",
            "market_identifier": atc4,
            "atc4_codes": [atc4],
            "definition_statements": [
                f"일반뷰 시장은 ATC4 코드 {atc4} 하나를 기준으로 정의됩니다."
            ],
            "selection_rationale": _unavailable_rationale(),
        }
        description = _text(row.get("atc4_desc"))
        if description:
            result["atc4_name"] = description
        return result

    def _landscape_definition(self, market_id: str, brand: str) -> dict[str, Any]:
        row = self._reader.market_landscape(market_id)
        if row is None:
            raise LookupError(f"전략시장 정의를 찾을 수 없습니다: {market_id}")
        market_name = _required_market_name(row, market_id)
        children = self._reader.competitive_markets(market_id)
        child_names = [
            _required_market_name(child, _text(child.get("cd_id")))
            for child in children
        ]
        brand_rows = self._reader.strategic_brands(
            brand=brand or None,
            market_id=market_id,
        )
        structure_rows = self._reader.strategic_brands(market_id=market_id)
        atc4_codes = _json_strings(row.get("atc_codes_json"))
        statements = [
            f"{market_name}는 MI Master 카탈로그에 기록된 market_landscape 전략뷰 시장입니다.",
            "이 전략뷰의 ATC4 범위는 " + ", ".join(atc4_codes) + "입니다.",
        ]
        if children:
            statements.append(
                "하위 competitive_dynamics 전략시장은 "
                + ", ".join(child_names)
                + "입니다."
            )
        result: dict[str, Any] = {
            "definition_type": "market_definition",
            "view_category": "전략뷰",
            "view_name": "market_landscape",
            "market_identifier": market_id,
            "market_name": market_name,
            "data_source": _source_label(row.get("data_source")),
            "atc4_codes": list(atc4_codes),
            "analysis_dimensions": _analysis_dimensions(row),
            "competitive_market_identifiers": [
                _text(child.get("cd_id")) for child in children
            ],
            "definition_statements": statements,
            "selection_rationale": _unavailable_rationale(),
        }
        result["definition_statements"].append(
            _analysis_statement(result["analysis_dimensions"], result["data_source"])
        )
        inclusion = _brand_inclusion(brand, brand_rows)
        if inclusion is not None:
            result["brand_inclusion"] = inclusion
            result["definition_statements"].append(_inclusion_statement(inclusion))
        structure = _class_structure(structure_rows)
        if structure is not None:
            result["class_structure"] = structure
            result["definition_statements"].extend(_class_structure_statements(structure))
        return result

    def _competitive_definition(self, market_id: str, brand: str) -> dict[str, Any]:
        row = self._reader.competitive_dynamics(market_id)
        if row is None:
            raise LookupError(f"경쟁구도 전략시장 정의를 찾을 수 없습니다: {market_id}")
        parent_id = _text(row.get("ml_id"))
        parent = self._reader.market_landscape(parent_id)
        market_name = _required_market_name(row, market_id)
        parent_name = _required_market_name(parent or {}, parent_id)
        brand_rows = self._reader.strategic_brands(
            brand=brand or None,
            market_id=parent_id,
            competitive_market_id=market_id,
        )
        structure_rows = self._reader.strategic_brands(
            market_id=parent_id,
            competitive_market_id=market_id,
        )
        rule_id = _text(row.get("cd_filter_id"))
        statements = [
            f"{market_name}는 {parent_name} market_landscape에서 범위를 좁힌 competitive_dynamics 전략뷰 시장입니다.",
            "좁힘 규칙의 조건 본문은 현재 런타임 카탈로그에서 조회되지 않습니다.",
        ]
        result: dict[str, Any] = {
            "definition_type": "market_definition",
            "view_category": "전략뷰",
            "view_name": "competitive_dynamics",
            "market_identifier": market_id,
            "market_name": market_name,
            "parent_market_identifier": parent_id,
            "parent_market_name": parent_name,
            "data_source": _source_label(row.get("data_source")),
            "analysis_dimensions": _analysis_dimensions(row),
            "narrowing_rule": {
                "available": False,
                "reference_id": rule_id,
                "message": "조건 본문은 현재 런타임 카탈로그에서 조회되지 않습니다.",
            },
            "definition_statements": statements,
            "selection_rationale": _unavailable_rationale(),
        }
        result["definition_statements"].append(
            _analysis_statement(result["analysis_dimensions"], result["data_source"])
        )
        inclusion = _brand_inclusion(brand, brand_rows)
        if inclusion is not None:
            result["brand_inclusion"] = inclusion
            result["definition_statements"].append(_inclusion_statement(inclusion))
        structure = _class_structure(structure_rows)
        if structure is not None:
            result["class_structure"] = structure
            result["definition_statements"].extend(_class_structure_statements(structure))
        return result

    def _competitive_children_definition(self, parent_market_id: str) -> dict[str, Any]:
        parent = self._reader.market_landscape(parent_market_id)
        if parent is None:
            raise LookupError(f"상위 전략시장 정의를 찾을 수 없습니다: {parent_market_id}")
        parent_name = _required_market_name(parent, parent_market_id)
        children = self._reader.competitive_markets(parent_market_id)
        child_ids = [_text(row.get("cd_id")) for row in children]
        child_names = [
            _required_market_name(row, _text(row.get("cd_id")))
            for row in children
        ]
        return {
            "definition_type": "market_definition",
            "view_category": "전략뷰",
            "view_name": "competitive_dynamics",
            "market_identifier": parent_market_id,
            "parent_market_identifier": parent_market_id,
            "parent_market_name": parent_name,
            "competitive_market_identifiers": child_ids,
            "definition_statements": [
                f"{parent_name} market_landscape에서 갈라지는 competitive_dynamics 전략뷰 시장은 "
                + ", ".join(child_names)
                + "입니다."
            ],
            "selection_rationale": _unavailable_rationale(),
        }


def _required_market_name(row: Mapping[str, object], market_id: str) -> str:
    name = _text(row.get("name"))
    if not name:
        raise LookupError(f"공개 시장명을 찾을 수 없습니다: {market_id}")
    return name
