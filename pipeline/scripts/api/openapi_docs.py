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
BRAND_ACTIVITY_TAG: Final = "Brand-Activity"
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


COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "cd_market_id": "cd_001",
        "view_kind": "competitive_dynamics",
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
                "examples": {
                    "market_landscape": {"summary": "전략 시장조망", "value": {"status": "SUCCESS", "result": CAUSE_RESPONSE_EXAMPLE}},
                    "competitive_dynamics": {
                        "summary": "전략 경쟁구도",
                        "value": {
                            "status": "SUCCESS",
                            "result": {
                                **CAUSE_RESPONSE_EXAMPLE,
                                "view": "competitive_dynamics",
                                "market_id": "cd_001",
                                "markets": [{"market_id": "cd_001", "is_primary": True}],
                            },
                        },
                    },
                },
            }
        },
    },
    400: {"description": "필터 조합, source, measure, market id가 유효하지 않음"},
}


BRAND_ACTIVITY_FILTER_EXAMPLE: Final = {
    "atc4": ["C10A1"],
    "molecule": ["PITAVASTATIN"],
    "channel": ["TOTAL"],
}


BRAND_ACTIVITY_TOPICS_REQUEST_EXAMPLE: Final = {
    "view": "general",
    "selected_brand": "리바로",
    "filters": BRAND_ACTIVITY_FILTER_EXAMPLE,
    "visit_location": "전체",
    "specialty": "전체",
    "top_n": 5,
}


BRAND_ACTIVITY_CSD_TIMESERIES_REQUEST_EXAMPLE: Final = {
    "view": "general",
    "selected_brand": "리바로",
    "filters": BRAND_ACTIVITY_FILTER_EXAMPLE,
    "mode": "absolute",
    "window": {"start": "2024Q1", "end": "2025Q4"},
}


BRAND_ACTIVITY_INTEREST_RX_REQUEST_EXAMPLE: Final = {
    "view": "general",
    "selected_brand": "리바로",
    "filters": BRAND_ACTIVITY_FILTER_EXAMPLE,
    "visit_location": "전체",
    "specialty": "전체",
    "period_start": "2024-01",
    "period_end": "2025-12",
    "weights": {
        "interest": {"VERY USEFUL": 1.0, "SOMEWHAT USEFUL": 0.5, "NOT AT ALL": 0.0},
        "rx_frequency": {"frequently": 1.0, "occasionally": 0.5},
    },
}


BRAND_ACTIVITY_SCOPE_SCHEMA: Final = {
    "type": "object",
    "description": "요청 view/시장/필터를 서버가 해석한 결과입니다. 화면의 적용 필터 칩과 차트 캡션에 사용합니다.",
    "properties": {
        "view": {"type": "string", "description": "general 또는 strategic_ml. 현재 CSD 서비스는 strategic_cd를 런타임에서 지원하지 않습니다."},
        "market_id": {"type": "string", "description": "해석된 시장 id. 일반뷰는 ATC4, 전략뷰는 ml_id입니다."},
        "market_name": {"type": "string", "description": "시장 표시명."},
        "resolved_market": {"type": "object", "description": "type/market_id/market_label/source로 구성된 resolved market echo."},
        "selected_brand": {"description": "선택 브랜드 또는 선택 브랜드 메타."},
        "applied_filter": {"type": "object", "description": "서버가 실제 적용한 필터."},
        "applied_filters": {"type": "object", "description": "applied_filter와 동일한 포탈 호환 alias."},
    },
}


BRAND_ACTIVITY_TOPICS_RESPONSES: Final = {
    200: {
        "description": "브랜드별 토픽 그리드. mock v0.1.7의 topics 계약을 운영 backend에 포팅한 공유 API입니다.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["data"],
                    "properties": {
                        "data": {
                            "type": ["object", "null"],
                            "description": "data.scope + data.brands. 시장 미해석 시 data=null과 reason을 반환할 수 있습니다.",
                            "properties": {
                                "scope": BRAND_ACTIVITY_SCOPE_SCHEMA,
                                "brands": {
                                    "type": "array",
                                    "description": "브랜드 카드 목록. topic_shares 합 + etc_pct = 100입니다.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "brand_key": {"type": "string", "description": "브랜드 식별 키."},
                                            "brand_name": {"type": "string", "description": "브랜드 표시명."},
                                            "is_jw": {"type": "boolean", "description": "JW 자사 브랜드 여부."},
                                            "is_selected": {"type": "boolean", "description": "선택 브랜드 여부."},
                                            "event_count": {"type": "integer", "description": "키워드 설문 응답 행 수 N."},
                                            "topic_shares": {"type": "array", "description": "상위 토픽 막대 목록(label/share_pct/topic_id/rank)."},
                                            "topics": {"type": "array", "description": "topic_shares와 같은 포탈 호환 alias."},
                                            "etc_pct": {"type": "number", "description": "상위 토픽 외 기타 비율."},
                                            "brand_specific_topics": {"type": "array", "description": "토픽 정의/근거 행 수를 포함한 상세 목록."},
                                        },
                                    },
                                },
                            },
                        },
                        "reason": {"type": "string", "description": "data가 null인 경우의 사유."},
                    },
                },
                "example": {
                    "data": {
                        "scope": {"view": "general", "market_id": "C10A1", "selected_brand": "리바로", "top_n": 5},
                        "brands": [
                            {
                                "brand_key": "리바로",
                                "brand_name": "리바로",
                                "is_jw": True,
                                "is_selected": True,
                                "event_count": 128,
                                "topic_shares": [{"rank": 1, "topic_id": "T01", "label": "당뇨 안전성/NODM", "share_pct": 62.5}],
                                "topics": [{"rank": 1, "topic_id": "T01", "label": "당뇨 안전성/NODM", "share_pct": 62.5}],
                                "etc_pct": 37.5,
                                "brand_specific_topics": [
                                    {
                                        "topic_id": "T01",
                                        "label": "당뇨 안전성/NODM",
                                        "definition": "피타바스타틴 안전성 메시지",
                                        "share_pct": 62.5,
                                        "row_count": 80,
                                    }
                                ],
                            }
                        ],
                    }
                },
            }
        },
    },
    400: {"description": "view/selected_brand/filter 조합이 유효하지 않음"},
}


BRAND_ACTIVITY_CSD_TIMESERIES_RESPONSES: Final = {
    200: {
        "description": (
            "활동·처방 추세. CSD 활동량은 csd_channel_dynamics_stage에서 jw_channel='TOTAL'(region=TOTAL)만 사용하며, "
            "처방 지표는 IQVIA mart의 unit/counting_unit/dosage_unit을 같은 분기축으로 맞춥니다."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["data"],
                    "properties": {
                        "data": {
                            "type": ["object", "null"],
                            "properties": {
                                "scope": {
                                    **BRAND_ACTIVITY_SCOPE_SCHEMA,
                                    "properties": {
                                        **BRAND_ACTIVITY_SCOPE_SCHEMA["properties"],
                                        "csd_market": {"type": "string", "description": "mart product code overlap으로 결정한 CSD 시장 표시명."},
                                        "quarters": {"type": "array", "items": {"type": "string"}, "description": "분기축. 예: 2025Q4."},
                                        "mode": {"type": "string", "description": "absolute 또는 share."},
                                    },
                                },
                                "brands": {
                                    "type": "array",
                                    "description": "브랜드별 activity/Rx series. is_selected 브랜드는 굵게, is_jw는 강조 표시 대상입니다.",
                                },
                                "market_totals": {"type": "object", "description": "activity와 Rx measure별 시장 총합 series."},
                            },
                        },
                        "reason": {"type": "string", "description": "data가 null인 경우의 사유."},
                    },
                },
                "example": {
                    "data": {
                        "scope": {
                            "view": "general",
                            "market_id": "C10A1",
                            "csd_market": "LIVALO",
                            "quarters": ["2025Q1", "2025Q2"],
                            "mode": "absolute",
                        },
                        "brands": [
                            {
                                "brand_key": "리바로",
                                "brand_name": "리바로",
                                "is_selected": True,
                                "is_jw": True,
                                "csd_matched": True,
                                "series": {
                                    "activity": {"source": "csd", "absolute": {"2025Q1": 120.0}, "ratio": {"2025Q1": 44.1}},
                                    "unit": {"source": "iqvia_nsa", "absolute": {"2025Q1": 1000.0}, "ratio": {"2025Q1": 20.5}},
                                },
                            }
                        ],
                        "market_totals": {"activity": {"2025Q1": 272.0}},
                    }
                },
            }
        },
    },
    400: {"description": "view/selected_brand/filter/window 조합이 유효하지 않음"},
}


BRAND_ACTIVITY_INTEREST_RX_RESPONSES: Final = {
    200: {
        "description": (
            "interest×처방빈도 버블. X=rx_frequency_score, Y=interest_score, 버블 면적=event_count. "
            "market_average는 차트 점선 십자 기준선입니다."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["data"],
                    "properties": {
                        "data": {
                            "type": ["object", "null"],
                            "properties": {
                                "scope": BRAND_ACTIVITY_SCOPE_SCHEMA,
                                "filters_applied": {"type": "object", "description": "visit_location/specialty/period window 적용 결과."},
                                "period": {"type": "object", "description": "키워드·CSD 공통 사용 가능 기간."},
                                "levels": {"type": "object", "description": "interest/rx_frequency/prescription_evolution 레벨 목록."},
                                "weights": {"type": "object", "description": "score 계산에 사용한 가중치. 미지정 시 서버 기본값."},
                                "brands": {"type": "array", "description": "브랜드별 distribution과 score."},
                                "market_average": {"type": "object", "description": "시장 평균 interest/rx score."},
                            },
                        },
                        "reason": {"type": "string", "description": "data가 null인 경우의 사유."},
                    },
                },
                "example": {
                    "data": {
                        "scope": {"view": "general", "market_id": "C10A1", "selected_brand": "리바로", "csd_market": "LIVALO"},
                        "period": {"start": "2024-01", "end": "2025-12", "source": "keyword_and_csd"},
                        "brands": [
                            {
                                "brand_key": "리바로",
                                "brand_name": "리바로",
                                "is_selected": True,
                                "is_jw": True,
                                "event_count": 42,
                                "interest_distribution": {"VERY USEFUL": 20, "SOMEWHAT USEFUL": 18, "NOT AT ALL": 4},
                                "rx_frequency_distribution": {"frequently": 12, "occasionally": 25},
                                "interest_score": 0.69,
                                "rx_frequency_score": 0.55,
                            }
                        ],
                        "market_average": {"interest_score": 0.58, "rx_frequency_score": 0.47},
                    }
                },
            }
        },
    },
    400: {"description": "view/selected_brand/filter/period 조합이 유효하지 않음"},
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
    "channel_axis": {
        "ubist": {
            "facility": [{"key": "종합병원", "value": "종합병원", "row_count": 120, "default": False, "selected": False, "flag": True}],
            "specialty": [{"key": "순환기(Cardiology IM)", "value": "순환기(Cardiology IM)", "row_count": 90, "default": False, "selected": False, "flag": True}],
            "pairs": [
                {
                    "key": "종합병원|순환기(Cardiology IM)",
                    "value": {"facility": "종합병원", "specialty": "순환기(Cardiology IM)"},
                    "row_count": 90,
                    "default": False,
                    "selected": False,
                    "flag": True,
                }
            ],
        },
        "iqvia": {
            "audit_code": [
                {"key": "KPA", "value": "KPA", "row_count": 120, "default": False, "selected": False, "flag": True},
                {"key": "KHPA", "value": "KHPA", "row_count": 90, "default": False, "selected": False, "flag": True},
            ]
        },
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
                        "channel_axis": {
                            "type": "object",
                            "description": (
                                "일반뷰 source별 채널 축 registry. UBIST는 facility(종별), specialty(진료과), "
                                "pairs(종별×진료과 조합)를 raw channel_specialty_matrix에서 동적으로 도출하고, "
                                "IQVIA는 audit_code를 raw audit_code_matrix에서 동적으로 도출합니다. "
                                "analysis_level과 분리된 값 슬라이스 필터입니다."
                            ),
                        },
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
