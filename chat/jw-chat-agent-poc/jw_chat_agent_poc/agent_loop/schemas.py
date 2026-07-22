from __future__ import annotations

import os
from typing import Any

from jw_chat_agent_poc.tools.query_layer import QueryCatalog


def tool_schemas(allowed_brands: tuple[str, ...], allowed_periods: tuple[str, ...], query_catalog: QueryCatalog | None = None) -> tuple[dict[str, Any], ...]:
    base = (
        _schema("get_metric", "브랜드 지표(매출, 점유율, 순위, HHI, UBIST 처방량)를 조회합니다.", ("brand", "measure"), allowed_brands, allowed_periods),
        _schema("get_market_scope", "브랜드가 속한 시장 scope와 같은 시장 브랜드 후보를 조회합니다.", ("brand",), allowed_brands, allowed_periods),
        _schema("resolve_relative_date", "3달전 같은 상대 날짜를 월 단위 period로 해석합니다.", ("expression",), allowed_brands, allowed_periods),
        _schema("search_news", "curated deep-analysis 뉴스/이슈를 브랜드와 텍스트 query로 검색합니다.", ("brand",), allowed_brands, allowed_periods),
        _schema("get_disease_stats", "브랜드의 확정 KCD 매핑 기반 HIRA 질병 환자 통계를 조회합니다.", ("brand",), allowed_brands, allowed_periods),
        _schema("get_procedure_stats", "HIRA 진료행위정보서비스에서 5단 행위코드(st5Cd) 기준 진료행위 통계를 조회합니다. 질문에 행위코드가 있을 때만 사용합니다.", ("brand", "query"), allowed_brands, allowed_periods),
        _schema("search_clinical", "브랜드 성분 기준 국내외 임상 근거를 조회하고 성분 범위 고지를 포함합니다.", ("brand",), allowed_brands, allowed_periods),
        _schema("get_clinical_study_details", "정확한 NCT ID로 ClinicalTrials.gov 상세를 조회합니다. 선정·제외 기준은 원문 앞 200자까지만 제공됨을 고지합니다.", ("nct_id",), allowed_brands, allowed_periods),
        _schema("search_patent", "브랜드 또는 확인된 성분 기준 특허/Orange Book 근거를 조회합니다. 브랜드가 없고 ingredient가 있으면 ingredient를 사용합니다.", ("query",), allowed_brands, allowed_periods),
        _schema("search_drug_info", "브랜드 기준 국내 식약처/MFDS 허가 품목 정보를 조회합니다. e약은요 경로는 사용하지 않습니다.", ("brand",), allowed_brands, allowed_periods),
        _schema("search_safety", "브랜드의 확정 성분 기준 FDA 라벨 안전성·이상반응 근거를 조회합니다.", ("brand",), allowed_brands, allowed_periods),
        _schema(
            "csd_activity_trend",
            "CSD ChannelDynamics stage에서 월별 TOTAL 채널 aggregate 콜수/활동량(product_details 합계)을 조회합니다. impact level, HCP/의사별, 기관별 세부는 포함하지 않습니다.",
            ("brand",),
            allowed_brands,
            allowed_periods,
        ),
        _schema("web_search", "내부 API가 덮지 못하는 외부 동향·디테일링·KOL 질문에 대해 웹 검색 결과를 URL/snippet으로 분리 조회합니다. 수치를 내부 fact로 승격하지 않습니다.", ("brand", "query"), allowed_brands, allowed_periods),
    )
    if query_catalog is None:
        return base
    return (*base, *_query_schemas(allowed_brands, allowed_periods, query_catalog))


def _schema(
    name: str,
    description: str,
    required: tuple[str, ...],
    allowed_brands: tuple[str, ...],
    allowed_periods: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": _properties(allowed_brands, allowed_periods),
                "required": list(required),
            },
        },
    }


def _properties(allowed_brands: tuple[str, ...], allowed_periods: tuple[str, ...]) -> dict[str, Any]:
    brand_schema: dict[str, Any] = {
        "type": "string",
        "description": "Canonical brand selected by code grounding; never free-type a Korean brand name.",
    }
    if allowed_brands:
        brand_schema["enum"] = list(allowed_brands)
    return {
        "brand": brand_schema,
        "measure": {
            "type": "string",
            "enum": ["sales", "market_share", "rank", "hhi", "series", "trend", "momentum", "ei", "growth", "prescription_volume"],
            "description": "prescription_volume is UBIST rx_qty volume in Rx units; it is not sales or prescription count.",
        },
        "period": {
            "type": "string",
            "enum": list(allowed_periods),
            "description": "Use only this code-grounded period enum; never invent unavailable months.",
        },
        "view": {"type": "string", "enum": _view_enum()},
        "source": {"type": "string", "description": "Optional mart source such as ubist or iqvia_nsa when the question explicitly asks for it."},
        "expression": {"type": "string"},
        "query": {"type": "string", "description": "Optional text query for news issue/search terms, not a brand."},
        "ingredient": {"type": "string", "description": "Code-grounded English ingredient for patent lookup when no canonical brand is present."},
        "nct_id": {"type": "string", "pattern": "^NCT[0-9]{8}$", "description": "Exact ClinicalTrials.gov NCT identifier."},
    }


def _view_enum() -> list[str]:
    values = ["market_landscape", "competitive_dynamics"]
    if os.environ.get("GENERAL_VIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        values.append("general_view")
    return values


def _query_schemas(allowed_brands: tuple[str, ...], allowed_periods: tuple[str, ...], catalog: QueryCatalog) -> tuple[dict[str, Any], ...]:
    props = _properties(allowed_brands, allowed_periods)
    props.update(
        {
            "comparison_brand": {"type": "string", "description": "Market member brand from the same strategic mart market; validated by code."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "history_points": {"type": "integer", "minimum": 2, "maximum": 60},
            "spec": _query_spec_schema(catalog),
        }
    )
    return (
        _schema_with_props(
            "get_brand_sales",
            "전략뷰 브랜드 매출을 조회합니다. 매출 변화의 시장 맥락을 위해 점유율·시장규모·성장률 근거와 함께 사용합니다.",
            ("brand",),
            props,
        ),
        _schema_with_props(
            "get_brand_share",
            "전략뷰 브랜드 점유율·순위를 조회합니다. 점유율만으로 매출 증감을 판단하지 말고 매출·시장규모·순위 근거와 함께 사용합니다.",
            ("brand",),
            props,
        ),
        _schema_with_props(
            "get_brand_series",
            "전략뷰 브랜드 월별 매출 또는 UBIST 처방량 시계열을 조회합니다. 처방량은 measure=prescription_volume으로 명시하며 매출과 합산하지 않습니다.",
            ("brand",),
            props,
        ),
        _schema_with_props("compare_brands_series", "query layer로 두 브랜드의 월별 매출·점유율 시계열을 비교합니다.", ("brand", "comparison_brand"), props),
        _schema_with_props(
            "get_top_brands",
            "전략뷰 시장 상위 브랜드·순위·분모를 조회합니다. 시장규모·HHI·CR5 근거와 함께 사용합니다.",
            ("brand",),
            props,
        ),
        _schema_with_props("get_brand_channel_breakdown", "query layer로 브랜드의 채널별 매출 또는 UBIST 처방량 구성을 조회합니다.", ("brand",), props),
        _schema_with_props("get_brand_specialty_breakdown", "query layer로 브랜드의 진료과별 매출 또는 UBIST 처방량 구성을 조회합니다.", ("brand",), props),
        _schema_with_props("query", "catalog enum에 맞는 query(spec)를 전략 mart에 실행합니다.", ("spec",), props),
    )


def _schema_with_props(name: str, description: str, required: tuple[str, ...], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


def _query_spec_schema(catalog: QueryCatalog) -> dict[str, Any]:
    enums = catalog.schema_fragment()
    return {
        "type": "object",
        "description": "Use only enum values from this catalog. Do not invent identifiers.",
        "properties": {
            "source": {"type": "string", "enum": list(enums["source"])},
            "view": {"type": "string", "enum": list(enums["view"])},
            "market": {"type": "string", "enum": list(enums["market"])},
            "dimensions": {"type": "array", "items": {"type": "string", "enum": list(enums["dimensions"])}},
            "group_by": {"type": "array", "items": {"type": "string", "enum": list(enums["group_by"])}},
            "metrics": {"type": "array", "items": {"type": "string", "enum": list(enums["metrics"])}},
            "derive": {"type": "array", "items": {"type": "string", "enum": list(enums["derive"])}},
            "sort": {"type": "string", "enum": list(enums["sort"])},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    }
