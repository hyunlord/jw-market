"""OpenAPI-only documentation helpers for the portal-shared API surface.

The constants in this module are consumed only by FastAPI's schema generator.
They must not be imported into service logic or used as ``response_model``
contracts, because the portal cause/dynamic payloads intentionally return
dict-shaped 23-section JSON and FastAPI response models would serialize away
unknown fields.
"""
# allow: SIZE_OK — OpenAPI schema/example constants only; no runtime business logic.

from __future__ import annotations

from typing import Final


PORTAL_CORE_TAG: Final = "Portal-Core"
DYNAMIC_MARKET_TAG: Final = "Dynamic-Market"
META_TAG: Final = "Meta"


CAUSE_RESPONSE_EXAMPLE: Final = {
    "brand": "리바로",
    "market_id": "strategy_006",
    "view": "market_landscape",
    "source": "UBIST",
    "measure": "sales",
    "unit_label": "억원",
    "markets": [{"market_id": "strategy_006", "is_primary": True}],
    "market_meta": {
        "market_id": "ml_006",
        "market_name": "리바로",
        "atc_codes": ["C10A1", "C10C0"],
    },
    "data": {
        "kpi": {"market_size_recent": 225677368890.9798, "market_yoy_recent_pct": 7.2576},
        "market_size_series": [{"period": "2026-04", "value": 225677368890.9798}],
        "analysis_levels": [],
        "level_top5_trend": {},
        "target_customer_competition": {},
        "ubist_specialty_channels": [],
    },
}


CAUSE_RESPONSE_SCHEMA: Final = {
    "type": "object",
    "description": (
        "포탈 원인분석 표준 응답입니다. data에는 운영 포탈이 렌더링하는 23개 섹션이 들어갑니다. "
        "브랜드가 해당 source에 없으면 data는 null이고 reason이 제공됩니다."
    ),
    "required": ["brand", "view", "source", "measure", "unit_label", "data"],
    "properties": {
        "brand": {"type": "string", "description": "요청 브랜드명 또는 표시 브랜드명", "example": "리바로"},
        "market_id": {"type": ["string", "null"], "description": "대표 전략 시장 id", "example": "strategy_006"},
        "view": {"type": "string", "description": "market_landscape 또는 competitive_dynamics", "example": "market_landscape"},
        "source": {"type": "string", "description": "UBIST 또는 IQVIA", "example": "UBIST"},
        "measure": {"type": "string", "description": "sales 또는 qty", "example": "sales"},
        "unit_label": {"type": "string", "description": "화면 표시 단위", "example": "억원"},
        "markets": {
            "type": "array",
            "description": "브랜드가 연결된 시장 목록. is_primary=true 항목이 대표 시장입니다.",
            "items": {
                "type": "object",
                "required": ["market_id", "is_primary"],
                "properties": {
                    "market_id": {"type": "string", "example": "strategy_006"},
                    "is_primary": {"type": "boolean", "example": True},
                },
            },
        },
        "market_meta": {
            "type": ["object", "null"],
            "description": "시장 정의 메타. ATC 코드, 시장명, catalog_definition 기반 설명을 포함합니다.",
        },
        "data": {
            "type": ["object", "null"],
            "description": (
                "원인분석 23섹션 payload. 주요 섹션: kpi, market_size_series, brand_ranking, "
                "company_ranking, analysis_levels, analysis_level_market_status, level_top5_trend, "
                "target_customer_competition, ubist_specialty_channels, market_meta."
            ),
        },
        "reason": {"type": "string", "description": "data가 null인 경우의 사유", "example": "brand_not_in_source"},
    },
    "example": CAUSE_RESPONSE_EXAMPLE,
}


CAUSE_RESPONSES: Final = {
    200: {
        "description": "포탈 원인분석 표준 응답",
        "content": {"application/json": {"schema": CAUSE_RESPONSE_SCHEMA, "example": CAUSE_RESPONSE_EXAMPLE}},
    },
    404: {"description": "브랜드가 cache_cause에 없음"},
}


DYNAMIC_MARKET_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "ml_id": "ml_006",
        "view_kind": "market_landscape",
        "analysis_level": {"ubist": {"atc4": ["C10A1"]}},
    },
    "options": {"top_n": 20},
}


DYNAMIC_MARKET_RESPONSES: Final = {
    200: {
        "description": (
            "status/result envelope. result는 /api/cause 응답과 같은 root 구조(markets, market_meta, data 23섹션)를 가집니다."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["status", "result"],
                    "properties": {
                        "status": {"type": "string", "enum": ["SUCCESS"], "example": "SUCCESS"},
                        "result": CAUSE_RESPONSE_SCHEMA,
                    },
                },
                "example": {"status": "SUCCESS", "result": CAUSE_RESPONSE_EXAMPLE},
            }
        },
    },
    400: {"description": "필터 조합, source, measure, market id가 유효하지 않음"},
}


FILTER_OPTIONS_EXAMPLE: Final = {
    "view": "strategic",
    "source": "ubist",
    "market_id": "ml_006",
    "brand": "리바로",
    "dimensions": [
        {
            "dimension_type": "class",
            "label": "class",
            "values": [{"key": "statin", "value": "Statin", "row_count": 10, "default": True, "selected": True, "flag": True}],
        }
    ],
    "atc": {
        "atc1": [{"key": "C", "value": "C", "label": "C", "level": "atc1", "parent": None, "default": True, "selected": True, "flag": True}],
        "atc2": [],
        "atc3": [],
        "atc4": [],
        "selectable_levels": ["atc3", "atc4"],
    },
    "default_selections": {"class": ["statin"], "atc1": ["C"]},
    "applied_selections": {"class": ["statin"], "atc1": ["C"]},
    "brand_matched": {"class": ["statin"], "atc4": ["C10A1"]},
}


FILTER_OPTIONS_RESPONSES: Final = {
    200: {
        "description": (
            "포탈 필터 옵션. 전략뷰는 시장 소속 옵션을 원샷으로, 일반뷰는 선택 ATC4 범위의 scoped 옵션을 반환합니다."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["view", "source", "market_id", "dimensions", "atc"],
                    "properties": {
                        "view": {"type": "string", "enum": ["strategic", "general"], "description": "필터 옵션 뷰"},
                        "source": {"type": "string", "enum": ["ubist", "iqvia"], "description": "공개 source"},
                        "market_id": {"type": ["string", "null"], "description": "전략 ml_id 또는 일반뷰 대표 ATC4"},
                        "brand": {"type": "string", "description": "입력 브랜드 echo"},
                        "dimensions": {
                            "type": "array",
                            "description": "registry 순서의 차원 목록. values의 flag=true는 선택 브랜드 해당 값입니다.",
                        },
                        "atc": {"type": "object", "description": "ATC1/2/3/4 계층. default/selected/flag 상태 포함."},
                        "default_selections": {"type": "object", "description": "초기 선택값. 차원 내 값은 OR입니다."},
                        "applied_selections": {"type": "object", "description": "현재 selections 입력을 반영한 선택값."},
                        "brand_matched": {"type": "object", "description": "브랜드 자신 값. 프론트에서 locked 처리합니다."},
                    },
                },
                "example": FILTER_OPTIONS_EXAMPLE,
            }
        },
    },
    400: {"description": "지원하지 않는 view/source 또는 잘못된 selections JSON"},
}


ATC_OPTIONS_RESPONSES: Final = {
    200: {
        "description": "일반뷰 1단계 또는 전략뷰 ATC narrowing용 ATC 계층 옵션",
        "content": {
            "application/json": {
                "example": {
                    "brand_name": "리바로",
                    "view": "general",
                    "source": "ubist",
                    "market_id": "C10A1",
                    "flagged_atc4": ["C10A1"],
                    "atc": {
                        "atc1": [{"key": "C", "level": "atc1", "parent": None, "flag": True}],
                        "atc2": [{"key": "C10", "level": "atc2", "parent": "C", "flag": True}],
                        "atc3": [{"key": "C10A", "level": "atc3", "parent": "C10", "flag": True}],
                        "atc4": [{"key": "C10A1", "level": "atc4", "parent": "C10A", "flag": True}],
                    },
                }
            }
        },
    },
    400: {"description": "지원하지 않는 view/source 또는 브랜드 ATC resolve 실패"},
}


HEALTH_RESPONSES: Final = {
    200: {
        "description": "서비스 상태와 로드된 cache 개수",
        "content": {
            "application/json": {
                "example": {"status": "ok", "markets_loaded": 25, "brands_loaded": 25, "version": "v0.9.44-filter-options-1be46df7-20260702"}
            }
        },
    }
}


BRANDS_RESPONSES: Final = {
    200: {
        "description": "포탈 브랜드 선택 목록",
        "content": {
            "application/json": {
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "brand": {"type": "string", "description": "브랜드명", "example": "리바로"},
                            "market_id": {"type": "string", "description": "대표 전략 시장 id", "example": "strategy_006"},
                            "market_name": {"type": "string", "description": "시장명", "example": "리바로"},
                            "sources": {"type": "array", "items": {"type": "string"}, "description": "사용 가능한 소스 목록"},
                            "atc_codes": {"type": "array", "items": {"type": "string"}, "description": "브랜드/시장 ATC 코드"},
                            "is_jw": {"type": "boolean", "description": "JW 제품 여부"},
                            "is_target": {"type": "boolean", "description": "전략 대상 브랜드 여부"},
                        },
                    },
                },
                "example": [
                    {
                        "brand": "리바로",
                        "market_id": "strategy_006",
                        "market_name": "리바로",
                        "sources": ["UBIST"],
                        "atc_codes": ["C10A1", "C10C0"],
                        "is_jw": True,
                        "is_target": True,
                    }
                ],
            }
        },
    }
}


MARKET_STATUS_RESPONSES: Final = {
    200: {
        "description": "포탈 시장 현황 카드 payload",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "description": "cache_market_status의 운영 payload. brand_cards 등 포탈 화면 섹션을 포함합니다.",
                },
                "example": {"brand_cards": [{"brand": "리바로", "market_id": "strategy_006", "source": "UBIST"}]},
            }
        },
    }
}


DEEP_ANALYSIS_RESPONSES: Final = {
    200: {
        "description": "포탈 심층분석 payload",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["brand", "data"],
                    "properties": {
                        "brand": {"type": "string", "description": "브랜드명", "example": "리바로"},
                        "generated_at": {"type": "string", "description": "KST ISO 생성 시각"},
                        "data": {
                            "type": "object",
                            "description": "forecast, ai_analysis 등 심층분석 화면 섹션.",
                        },
                    },
                },
                "example": {"brand": "리바로", "generated_at": "2026-07-03T01:30:00+09:00", "data": {"ai_analysis": {}}},
            }
        },
    },
    404: {"description": "브랜드 심층분석 cache 없음"},
}
