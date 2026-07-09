"""OpenAPI-only documentation helpers for the portal-shared API surface.

The constants in this module are consumed only by FastAPI's schema generator.
They must not be imported into service logic or used as ``response_model``
contracts, because the portal cause/dynamic payloads intentionally return
dict-shaped 23-section JSON and FastAPI response models would serialize away
unknown fields.
"""
# allow: SIZE_OK — OpenAPI schema/example constants only; no runtime business logic.

from __future__ import annotations

from copy import deepcopy
from typing import Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


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


DYNAMIC_MARKET_DESCRIPTION: Final = """
`/api/dynamic-market`는 포탈 원인분석 payload를 캐시 없이 재계산하는 POST API입니다.
응답은 항상 `status`/`result` envelope이며, `result`는 `/api/cause/{brand}`가 돌려주는
root 구조(`brand`, `market_id`, `market_meta`, `data`)와 같은 모양입니다.

### 요청 body 최상위 필드와 원칙

| 필드 | 타입 | 필수 | 기본값 | missing 처리 | null 처리 |
|---|---|---:|---|---|---|
| `source` | string | 아니오 | `ubist` | `ubist`로 계산 | 422 validation error |
| `measure` | string | 아니오 | `sales` | `sales`로 계산 | 422 validation error |
| `filters` | object | 아니오 | 빈 필터 객체 | 빈 필터 객체 | 422 validation error |
| `options` | object | 아니오 | `{top_n:20, period_range:null}` | 기본 옵션 객체 | 422 validation error |

`source`는 `ubist`, `iqvia`, `iqvia_nsa`, `nsa`를 받을 수 있고 내부에서는 `iqvia`/`nsa`가
`iqvia_nsa`로 정규화됩니다. `measure`는 UBIST에서 `sales`, `volume`, IQVIA에서
`sales`, `unit`, `counting_unit`, `dosage_unit`만 유효합니다.

빈 `analysis_level` 차원은 그 차원을 적용하지 않는 전체 선택(select-all)입니다. 공통 시장 범위인
top-level `filters.atc4`는 일반뷰와 전략뷰가 모두 사용합니다. 일반뷰에서 `filters.atc4`를 생략하고
`focus_brand_key`를 보내면, 해당 브랜드가 속한 모든 ATC4를 합집합으로 사용합니다.
전략뷰에서 `filters.atc4`를 생략하거나 빈 배열로 보내면 선택된 전략 시장 전체를 사용합니다.
일반뷰에서 `focus_brand_key`와 `filters.atc4`가 모두 없으면 시장 범위를 정할 수 없어 400입니다.

### 공통 `filters` 필드

| 필드 | 타입 | 기본값 | 동작 |
|---|---|---|---|
| `atc4(ATC4 시장 범위/narrowing)` | string[] | `[]` | 일반뷰에서는 시장 scope, 전략뷰에서는 ML/CD 시장 안의 ATC narrowing입니다. 생략/빈 배열은 select-all입니다. |
| `view_kind` | string/null | null | `market_landscape`/`strategic_ml`/`ml`은 ML 전략뷰, `competitive_dynamics`/`strategic_cd`/`cd`는 CD 전략뷰입니다. 값이 있으면 전략뷰 분기로 들어갑니다. |
| `focus_brand_key` | string/null | null | `filters.atc4` 생략 시 브랜드 기준 ATC4 합집합을 만드는 데 사용합니다. 빈 문자열은 대부분 미입력처럼 처리됩니다. |
| `analysis_level` | object | 빈 source 객체 | 소스별 필터 딕셔너리입니다. row filter와 값 슬라이스를 같은 source 하위에 넣습니다. |

`filters` 자체를 생략하면 빈 객체로 처리됩니다. `filters:null`은 허용되지 않습니다.
중첩 list 필드는 생략하면 `[]`, `null`이면 422, 빈 list이면 적용하지 않습니다.
선택 string 필드(`view_kind`, `focus_brand_key`)는 missing과 null이 모두 `None`이며,
빈 문자열은 resolver의 truthy/strip 조건에 따라 미입력 또는 잘못된 id로 처리될 수 있으므로 보내지 않는 것을 권장합니다.

### 일반뷰 UBIST `analysis_level.ubist`

허용 키: `atc3(ATC3 좁히기)`, `atc4(ATC4 좁히기)`, `seller(판매사)`,
`molecule_strength(성분용량)`, `form(제형)`, `route(투여경로)`, `reimbursement(급여구분)`,
`facility(종별)`, `specialty(진료과)`, `pairs(종별×진료과 pair)`.

### 일반뷰 IQVIA `analysis_level.iqvia`

허용 키: `mfr_name_kor(제조사명)`, `molecule_type(성분구분)`, `molecule_desc(성분명)`,
`pack_desc(PACK DESC)`, `strength(함량)`, `nhi_type(NHI 구분)`, `audit_code(IQVIA audit code)`.
모든 일반뷰 IQVIA 분석레벨 필터는 같은 `analysis_level.iqvia` 객체에서 함께 보냅니다.
`pack_desc`도 같은 입력 필드이며, 내부 canonical dimension_type은 `pack`입니다.
`audit_code`는 row filter가 아니라 raw `audit_code_matrix` 값 슬라이스입니다. missing/빈 배열이면 전체 audit code를 포함합니다.

### 전략뷰 필터

전략뷰도 top-level `filters.atc4` 하나로 ATC narrowing을 합니다. `analysis_level.<source>.atc3` 또는
`analysis_level.<source>.atc4`는 전략뷰 narrowing 입력이 아니며 active 값이 있으면 400입니다.
`class(클래스)`, `mfr/mfr_name_kor(제조사)`, `nhi/nhi_type(NHI 구분)`, molecule/pack/strength/form/route/reimbursement 계열도
전략뷰 요청 필터가 아닙니다. `facility`, `specialty`, `pairs`, `audit_code` 같은 값 슬라이스 필드도 일반뷰 전용이므로
전략뷰에서 active 값이 있으면 400입니다.

전략뷰에서 ATC를 전체 선택하려면 `filters.atc4`를 생략하거나 빈 배열로 보내면 됩니다.
전략 시장 id(`ml_id`, `cd_market_id`)는 공개 요청 필드가 아닙니다. `focus_brand_key`와 `view_kind`만 보내면
백엔드가 선택한 `source`/`measure`에서 브랜드가 속한 ML 또는 CD 시장을 조회합니다. 같은 브랜드가 여러 시장에
속하면 시장 id 오름차순 첫 번째를 결정론적으로 사용합니다. 예를 들어 `ml_005, ml_008`이면 `ml_005`,
`cd_006, cd_007`이면 `cd_006`입니다. `ml_id`나 `cd_market_id`를 요청에 포함하면 schema extra-forbid로
422 validation error가 납니다.

다른 source의 객체에 값이 있으면 400입니다. 예를 들어 `source:"iqvia"` 요청에서
`analysis_level.ubist.seller`에 값이 있으면 `analysis_level must match selected source`가 반환됩니다.

### `options`

`top_n`은 기본 20이고 1~100 범위입니다. `top_n:null`은 런타임에서 20으로 보정됩니다.
`period_range.start/end`는 선택 기간 경계입니다.
`period_range`를 생략하거나 null이면 전체 기간을 사용합니다. `period_range:{}`는 시작/끝 모두 없는 전체 기간과 같습니다.
경쟁 브랜드 표시는 선택된 시장 필터 scope 안에서 `period_range` 적용 후 매출 합계(`total_value`) 기준으로 정렬합니다.
`focus_brand_key`가 있으면 선택 브랜드를 항상 첫 번째로 포함하고, 나머지는 매출 합계 내림차순/`brand_key` 오름차순으로 채웁니다.

### 응답 구조

성공 시 `result.data`에는 포탈 원인분석 섹션이 들어갑니다. 대표 키는 `kpi`, `market_size_series`,
`brand_ranking`, `company_ranking`, `analysis_levels`, `analysis_level_market_status`,
`level_top5_trend`, `target_customer_competition`, `target_customer_competition_by_channel`,
`ubist_specialty_channels`, `ubist_specialty_target_channels`입니다. 해당 source/범위에 데이터가 없거나
채널축이 없으면 빈 배열(`[]`), 빈 객체(`{}`), 또는 `note`가 있는 fallback 객체로 반환됩니다.

요청 검증 실패는 대부분 400 `detail.error=invalid_dynamic_market_request`입니다.
scope가 너무 넓으면 400 `detail.error=dynamic_scope_too_broad`와 `resolved_brand_rows`, `limit`가 함께 반환됩니다.
Pydantic 타입 검증 실패(null을 허용하지 않는 필드에 null 등)는 422입니다.

### Brand-Activity와의 필터 관계

`/api/brand-activity/*`도 같은 시장 필터 개념을 쓰지만 request model은 별도입니다.
다만 같은 Pydantic 클래스를 공유하지는 않습니다. Dynamic-Market은 알 수 없는 필드를 `extra=forbid`로 거절하지만,
Brand-Activity는 중첩 필터 모델이 extra 값을 허용합니다. 실제 Brand-Activity service handler는 일반뷰 시장 id를
flat `filters.atc4`에서 읽으므로, Pydantic 모델에 보이는 nested `filters.atc.atc4`만 보내면 400
(`filters.atc4 and selected_brand are required`)이 날 수 있습니다. Brand-Activity에서는 `filters`가 비어 있으면
legacy `filter`를 대신 쓰며, 둘 다 비어 있으면 빈 필터로 처리됩니다.
"""


PUBLIC_GENERAL_UBIST_ANALYSIS_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "atc3": {"type": "array", "items": {"type": "string"}, "description": "atc3(ATC3 좁히기)"},
        "atc4": {"type": "array", "items": {"type": "string"}, "description": "atc4(ATC4 좁히기)"},
        "seller": {"type": "array", "items": {"type": "string"}, "description": "seller(판매사)"},
        "molecule_strength": {"type": "array", "items": {"type": "string"}, "description": "molecule_strength(성분용량)"},
        "form": {"type": "array", "items": {"type": "string"}, "description": "form(제형)"},
        "route": {"type": "array", "items": {"type": "string"}, "description": "route(투여경로)"},
        "reimbursement": {"type": "array", "items": {"type": "string"}, "description": "reimbursement(급여구분)"},
        "facility": {"type": "array", "items": {"type": "string"}, "description": "facility(종별) 값 슬라이스"},
        "specialty": {"type": "array", "items": {"type": "string"}, "description": "specialty(진료과) 값 슬라이스"},
        "pairs": {
            "type": "array",
            "description": "pairs(종별×진료과 pair) 값 슬라이스",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"facility": {"type": "string"}, "specialty": {"type": "string"}},
                "required": ["facility", "specialty"],
            },
        },
    },
}


PUBLIC_GENERAL_IQVIA_ANALYSIS_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mfr_name_kor": {"type": "array", "items": {"type": "string"}, "description": "mfr_name_kor(제조사명)"},
        "molecule_type": {"type": "array", "items": {"type": "string"}, "description": "molecule_type(성분구분)"},
        "molecule_desc": {"type": "array", "items": {"type": "string"}, "description": "molecule_desc(성분명)"},
        "pack_desc": {"type": "array", "items": {"type": "string"}, "description": "pack_desc(PACK DESC)"},
        "strength": {"type": "array", "items": {"type": "string"}, "description": "strength(함량)"},
        "nhi_type": {"type": "array", "items": {"type": "string"}, "description": "nhi_type(NHI 구분)"},
        "audit_code": {
            "type": "array",
            "items": {"type": "string"},
            "description": "audit_code(IQVIA audit code). 비어 있으면 전체 audit code 포함",
        },
    },
}


PUBLIC_DYNAMIC_MARKET_REQUEST_SCHEMA: Final = {
    "oneOf": [
        {
            "title": "General UBIST dynamic-market request",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "source": {"type": "string", "enum": ["ubist"], "default": "ubist"},
                "measure": {"type": "string", "description": "measure(지표): sales 또는 volume"},
                "filters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "focus_brand_key": {"type": "string", "description": "focus_brand_key(선택 브랜드)"},
                        "atc4": {"type": "array", "items": {"type": "string"}, "description": "atc4(일반뷰 ATC4 시장 범위)"},
                        "analysis_level": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"ubist": PUBLIC_GENERAL_UBIST_ANALYSIS_SCHEMA},
                        },
                    },
                },
                "options": {"$ref": "#/components/schemas/DynamicMarketOptions"},
            },
        },
        {
            "title": "General IQVIA dynamic-market request",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "source": {"type": "string", "enum": ["iqvia", "iqvia_nsa", "nsa"], "default": "iqvia"},
                "measure": {"type": "string", "description": "measure(지표): sales, unit, counting_unit, dosage_unit"},
                "filters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "focus_brand_key": {"type": "string", "description": "focus_brand_key(선택 브랜드)"},
                        "atc4": {"type": "array", "items": {"type": "string"}, "description": "atc4(일반뷰 ATC4 시장 범위)"},
                        "analysis_level": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"iqvia": PUBLIC_GENERAL_IQVIA_ANALYSIS_SCHEMA},
                        },
                    },
                },
                "options": {"$ref": "#/components/schemas/DynamicMarketOptions"},
            },
        },
        {
            "title": "Strategic Market Landscape / Competitive Dynamics request",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "source": {"type": "string", "enum": ["ubist", "iqvia", "iqvia_nsa", "nsa"]},
                "measure": {"type": "string", "description": "measure(지표)"},
                "filters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "focus_brand_key": {"type": "string", "description": "focus_brand_key(선택 브랜드)"},
                        "view_kind": {"type": "string", "enum": ["market_landscape", "competitive_dynamics", "strategic_ml", "strategic_cd", "ml", "cd"]},
                        "atc4": {"type": "array", "items": {"type": "string"}, "description": "ATC4 전략뷰 narrowing. 생략/빈 배열이면 전략 시장 전체 선택."},
                    },
                },
                "options": {"$ref": "#/components/schemas/DynamicMarketOptions"},
            },
        },
    ]
}


DYNAMIC_MARKET_REQUEST_BODY_DESCRIPTION: Final = {
    "description": "동적 원인분석 요청입니다. view/source별 허용 필드는 schema oneOf와 endpoint 설명을 참조하십시오.",
    "content": {
        "application/json": {
            "schema": PUBLIC_DYNAMIC_MARKET_REQUEST_SCHEMA,
            "examples": {},
        }
    },
}


DYNAMIC_MARKET_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "view_kind": "market_landscape",
        "atc4": ["C10A1"],
    },
    "options": {"top_n": 20},
}


GENERAL_BASELINE_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "atc4": ["C10A1"],
    },
    "options": {"top_n": 20},
}


GENERAL_UBIST_FILTER_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "가드렛",
        "atc4": ["A10N3"],
        "analysis_level": {
            "ubist": {
                "seller": ["JW중외제약"],
                "molecule_strength": ["anagliptin 100mg"],
                "form": ["정제"],
                "route": ["경구"],
                "reimbursement": ["급여"],
                "facility": ["의원"],
                "specialty": ["내분비"],
            }
        },
    },
    "options": {"top_n": 10, "period_range": {"start": "2024-01", "end": "2026-04"}},
}


GENERAL_IQVIA_FILTER_REQUEST_EXAMPLE: Final = {
    "source": "iqvia",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "악템라",
        "atc4": ["M01C0"],
        "analysis_level": {
            "iqvia": {
                "mfr_name_kor": ["제이더블유중외제약"],
                "molecule_desc": ["TOCILIZUMAB"],
                "molecule_type": ["SINGLE"],
                "pack_desc": ["PRE-F SRN SC 162MG 0.9ML"],
                "strength": ["162MG"],
                "nhi_type": ["NHI"],
                "audit_code": ["KHPA", "KPA"],
            }
        },
    },
    "options": {"top_n": 10, "period_range": {"start": "2024-Q1", "end": "2026-Q1"}},
}


COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "view_kind": "competitive_dynamics",
    },
    "options": {"top_n": 20},
}


DYNAMIC_MARKET_REQUEST_EXAMPLES: Final = {
    "general_baseline": {
        "summary": "일반뷰 기본 조회: ATC4만 지정",
        "description": "UBIST 일반뷰에서 명시한 ATC4 범위를 계산합니다. ATC4를 생략하면 focus_brand_key의 ATC4 전체가 범위가 됩니다.",
        "value": GENERAL_BASELINE_REQUEST_EXAMPLE,
    },
    "general_ubist_filters": {
        "summary": "일반뷰 UBIST 분석레벨+채널축",
        "description": "UBIST에서 seller/form/route/reimbursement 등 UBIST 전용 필터와 specialty 채널축을 함께 적용합니다.",
        "value": GENERAL_UBIST_FILTER_REQUEST_EXAMPLE,
    },
    "general_iqvia_filters": {
        "summary": "일반뷰 IQVIA 분석레벨+PACK DESC+audit_code",
        "description": "IQVIA는 mfr_name_kor/molecule_desc/molecule_type/pack_desc/strength/nhi_type과 audit_code 값 슬라이스를 같은 analysis_level.iqvia 객체에 함께 보냅니다.",
        "value": GENERAL_IQVIA_FILTER_REQUEST_EXAMPLE,
    },
    "market_landscape": {
        "summary": "전략뷰 Market Landscape: 브랜드명 기반 자동 시장 결정 + ATC narrowing",
        "description": "focus_brand_key와 view_kind로 ML 시장을 내부 조회하고, 필요하면 top-level filters.atc4로 추가 narrowing합니다.",
        "value": DYNAMIC_MARKET_REQUEST_EXAMPLE,
    },
    "competitive_dynamics": {
        "summary": "전략뷰 Competitive Dynamics: 브랜드명 기반 자동 시장 결정",
        "description": "focus_brand_key와 view_kind만 보내면 CD 시장을 내부 조회합니다. 모호하면 cd_market_id 오름차순 첫 번째를 사용합니다.",
        "value": COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE,
    },
}

DYNAMIC_MARKET_REQUEST_BODY_DESCRIPTION["content"]["application/json"]["examples"] = DYNAMIC_MARKET_REQUEST_EXAMPLES


DYNAMIC_MARKET_ERROR_EXAMPLES: Final = {
    "unsupported_filter_key": {
        "summary": "지원하지 않는 필터 키",
        "value": {
            "detail": {
                "error": "invalid_dynamic_market_request",
                "message": "unsupported analysis_level dimension for iqvia_nsa: unknown_dimension",
            }
        },
    },
    "source_mismatch": {
        "summary": "source와 analysis_level 객체 불일치",
        "value": {
            "detail": {
                "error": "invalid_dynamic_market_request",
                "message": "analysis_level must match selected source: iqvia_nsa",
            }
        },
    },
    "scope_too_broad": {
        "summary": "브랜드 범위 과대",
        "value": {
            "detail": {
                "error": "dynamic_scope_too_broad",
                "message": "dynamic market scope resolved too many brand rows",
                "resolved_brand_rows": 250,
                "limit": 200,
            }
        },
    },
}


DYNAMIC_MARKET_RESPONSES: Final = {
    200: {
        "description": (
            "status/result envelope. result는 /api/cause 응답과 같은 root 구조(markets, market_meta, data 23섹션)를 가집니다. "
            "데이터가 없는 섹션은 source/필터 조건에 따라 [] 또는 {} 또는 note 포함 fallback 객체로 반환됩니다."
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
    400: {
        "description": (
            "필터 조합, source, measure, market id가 유효하지 않습니다. "
            "응답 detail.error는 invalid_dynamic_market_request 또는 dynamic_scope_too_broad입니다."
        ),
        "content": {"application/json": {"examples": DYNAMIC_MARKET_ERROR_EXAMPLES}},
    },
}


BRAND_ACTIVITY_FILTER_EXAMPLE: Final = {
    "atc": {"atc4": ["C10A1"]},
    "analysis_level": {
        "iqvia": {
            "mfr_name_kor": ["JW중외제약"],
            "molecule_desc": ["PITAVASTATIN"],
            "pack_desc": ["TAB 2MG 30S"],
            "strength": ["2MG"],
            "audit_code": ["KPA", "KHPA"],
        },
    },
    "channel": {
        "visit_location": ["의원"],
        "specialty": ["순환기(Cardiology IM)"],
    },
}


BRAND_ACTIVITY_FILTER_DESCRIPTION: Final = """
Brand-Activity 3종은 Dynamic-Market과 같은 시장 필터 개념을 쓰지만 request model은 별도입니다.

- **source 입력은 없습니다.** Rx/브랜드 랭킹 쪽은 서버 코드의 `iqvia_nsa` source를 사용하고, 활동·키워드 쪽은 CSD/keyword 테이블을 결합합니다.

- **시장 범위는 ATC4입니다.** `filters.atc4`를 보내거나, BFF 호환 입력인 `filters.atc.atc4`를 보내면 서버가 flat `filters.atc4`로 정규화합니다.

- **Brand-Activity는 IQVIA 전용입니다.** 상위 경쟁 브랜드 5개를 뽑을 때 Dynamic-Market 일반뷰 IQVIA와 같은 6개 row 필터(`mfr_name_kor`, `molecule_type`, `molecule_desc`, `pack_desc`, `strength`, `nhi_type`)를 적용합니다. 각 차원 안에서는 OR, 차원끼리는 AND입니다.

- **경쟁 브랜드 기준은 Dynamic-Market과 같습니다.** 선택된 시장 필터 scope 안에서 매출 합계 기준 상위 5개에 선택 브랜드를 항상 포함해 최대 6개를 반환합니다. CSD 계열처럼 quarter window가 있는 요청은 해당 window 합계, window가 없는 요청은 mart metric history 전체 합계를 사용하며 tie는 `brand_key` 오름차순입니다.

- **IQVIA audit code는 채널축 값 슬라이스입니다.** `filters.analysis_level.iqvia.audit_code`로 보내며, 옛 호환 입력 `filters.channel.audit_code`도 같은 값으로 정규화됩니다. 이 값은 경쟁 브랜드 선정 시 선택된 window의 audit code 매출 합계에 반영됩니다.

- **키워드 행 필터는 별도 입력입니다.** `visit_location`, `specialty`, `interest`, `prescription_evolution`, `period_start`, `period_end`는 토픽/interest 행을 자르는 필터입니다. `filters.channel.visit_location`과 `filters.channel.specialty`도 호환 입력으로 flat 필드에 정규화됩니다.

- **missing/null 처리:** `filters`와 `filter`를 생략하면 빈 필터 객체입니다. `filters:null` 또는 `filter:null`은 validation error입니다. `filters`와 legacy `filter`를 둘 다 보내면 비어 있지 않은 `filters`가 우선합니다.

- **unknown field 처리:** Brand-Activity request top-level은 알 수 없는 필드를 무시하고, 중첩 필터 객체는 호환성을 위해 추가 필드를 보존할 수 있습니다.

- **PACK DESC:** `pack_desc`는 canonical sidecar의 `dimension_type='pack'` 행과 매칭해 상위 경쟁 브랜드 후보를 좁힙니다.

`channel_axis` 입력은 공개 요청 스키마에서 제거됐고 validation error로 거절됩니다.
"""


BRAND_ACTIVITY_IQVIA_ANALYSIS_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mfr_name_kor": {
            "type": "array",
            "items": {"type": "string"},
            "description": "제조사명(MFR NAME KOR) row 필터. 차원 내 OR, 다른 IQVIA 차원과 AND.",
        },
        "molecule_type": {
            "type": "array",
            "items": {"type": "string"},
            "description": "성분 타입(MOLECULE TYPE) row 필터. 예: SINGLE, COMBINE.",
        },
        "molecule_desc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "성분명(MOLECULE DESC) row 필터.",
        },
        "pack_desc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "PACK DESC row 필터. canonical sidecar의 dimension_type=pack 값을 사용합니다.",
        },
        "strength": {
            "type": "array",
            "items": {"type": "string"},
            "description": "STRENGTH row 필터.",
        },
        "nhi_type": {
            "type": "array",
            "items": {"type": "string"},
            "description": "NHI TYPE row 필터.",
        },
        "audit_code": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IQVIA audit code 값 슬라이스. 예: KPA, KHPA. 생략하거나 빈 배열이면 전체 audit code입니다.",
        },
    },
}


BRAND_ACTIVITY_FILTER_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "atc4": {"type": "array", "items": {"type": "string"}, "description": "일반뷰 시장 ATC4. 예: C10A1."},
        "atc": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "atc4": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BFF 호환 nested ATC4. 서버가 filters.atc4로 정규화합니다.",
                },
            },
        },
        "analysis_level": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"iqvia": BRAND_ACTIVITY_IQVIA_ANALYSIS_SCHEMA},
            "description": "Brand-Activity 공개 필터는 IQVIA 전용입니다. 6개 row dimension은 상위 경쟁 브랜드 후보를 좁히고, audit_code는 경쟁 브랜드 매출 합계 산정 값을 채널축으로 슬라이스합니다.",
        },
        "channel": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "audit_code": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Legacy IQVIA audit_code shortcut. analysis_level.iqvia.audit_code로 정규화합니다.",
                },
                "visit_location": {"type": "array", "items": {"type": "string"}, "description": "Legacy 키워드 종별 행 필터."},
                "specialty": {"type": "array", "items": {"type": "string"}, "description": "Legacy 키워드 진료과 행 필터."},
            },
        },
        "visit_location": {"type": "array", "items": {"type": "string"}, "description": "키워드 종별 행 필터."},
        "specialty": {"type": "array", "items": {"type": "string"}, "description": "키워드 진료과 행 필터."},
        "interest": {"type": "array", "items": {"type": "string"}, "description": "키워드 관심도 행 필터."},
        "prescription_evolution": {"type": "array", "items": {"type": "string"}, "description": "처방 변화 행 필터."},
        "period_start": {"type": "string", "description": "행 필터 시작월 YYYY-MM."},
        "period_end": {"type": "string", "description": "행 필터 종료월 YYYY-MM."},
    },
}


BRAND_ACTIVITY_BASE_REQUEST_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": True,
    "required": ["selected_brand"],
    "properties": {
        "view": {"type": "string", "enum": ["general", "strategic_ml"], "default": "general"},
        "selected_brand": {"type": "string", "description": "선택 브랜드. 예: 리바로."},
        "filters": BRAND_ACTIVITY_FILTER_SCHEMA,
        "filter": {**BRAND_ACTIVITY_FILTER_SCHEMA, "description": "Legacy 단수 필터 입력. 신규 호출은 filters를 사용합니다."},
    },
}


def brand_activity_request_body(extra_properties: dict[str, object], example: dict[str, object]) -> dict[str, object]:
    return {
        "content": {
            "application/json": {
                "schema": {
                    **BRAND_ACTIVITY_BASE_REQUEST_SCHEMA,
                    "properties": {
                        **BRAND_ACTIVITY_BASE_REQUEST_SCHEMA["properties"],
                        **extra_properties,
                    },
                },
                "example": example,
            }
        }
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
        "visit_location": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "토픽 행 필터의 종별 표시값. 단일 선택은 문자열, 다중 선택은 문자열 배열, 미선택은 `전체`.",
        },
        "specialty": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "토픽 행 필터의 진료과 표시값. 단일 선택은 문자열, 다중 선택은 문자열 배열, 미선택은 `전체`.",
        },
        "interest": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "토픽 행 필터의 관심도 표시값. 단일 선택은 문자열, 다중 선택은 문자열 배열, 미선택은 `전체`.",
        },
        "prescription_evolution": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "토픽 행 필터의 처방 변화 표시값. 단일 선택은 문자열, 다중 선택은 문자열 배열, 미선택은 `전체`.",
        },
        "period_start": {"type": "string", "description": "토픽 행 필터 시작월 YYYY-MM. 미지정 시 빈 문자열."},
        "period_end": {"type": "string", "description": "토픽 행 필터 종료월 YYYY-MM. 미지정 시 빈 문자열."},
        "top_n": {"type": "integer", "description": "브랜드 카드에 산출한 상위 토픽 개수. 요청값은 1~10으로 clamp됩니다."},
        "sliced": {"type": "boolean", "description": "토픽 행 필터가 적용되어 row-topic assignment를 slicing했는지 여부."},
        "applied_topic_filters": {
            "type": "object",
            "description": "실제로 적용된 토픽 행 필터. visit_location/specialty/interest/prescription_evolution은 배열, 기간은 문자열입니다.",
        },
        "topic_set_version": {"type": ["string", "null"], "description": "선택된 topic scope의 버전. scope가 없으면 null."},
        "filter_effect": {
            "type": "object",
            "description": "`brand_set`(base 또는 channel_axis_applied)과 `payload`(filtered/unfiltered assignment 경로)를 담은 필터 효과 echo.",
        },
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
                                            "sales_rank": {"type": ["integer", "null"], "description": "시장 내 매출 rank. rank를 산출할 수 없으면 null."},
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
                        "scope": {
                            "view": "general",
                            "market_id": "C10A1",
                            "selected_brand": "리바로",
                            "top_n": 5,
                            "visit_location": "전체",
                            "specialty": "전체",
                            "interest": "전체",
                            "prescription_evolution": "전체",
                            "period_start": "",
                            "period_end": "",
                            "sliced": False,
                            "applied_topic_filters": {},
                            "topic_set_version": "v1",
                            "filter_effect": {"brand_set": "base", "payload": "row_topic_assignment_unfiltered"},
                        },
                        "brands": [
                            {
                                "brand_key": "리바로",
                                "brand_name": "리바로",
                                "is_jw": True,
                                "is_selected": True,
                                "sales_rank": 1,
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
            "활동·처방 추세. CSD 활동량은 csd_channel_dynamics_stage에서 jw_channel='TOTAL'(region=TOTAL)만 사용하며 월간 activity_months 축으로 반환합니다. "
            "IQVIA mart의 sales/unit/counting_unit/dosage_unit은 기존 quarters 분기축을 유지합니다."
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
                                        "quarters": {"type": "array", "items": {"type": "string"}, "description": "Rx measure 분기축. 예: 2025-Q4."},
                                        "activity_months": {"type": "array", "items": {"type": "string"}, "description": "CSD activity 월간축. 요청 분기 window 안의 YYYY-MM 목록."},
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
                            "quarters": ["2025-Q1", "2025-Q2"],
                            "activity_months": ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"],
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
                                    "activity": {"source": "csd", "absolute": {"2025-01": 40.0, "2025-02": 45.0}, "ratio": {"2025-01": 44.1, "2025-02": 45.3}},
                                    "sales": {"source": "iqvia_nsa", "absolute": {"2025-Q1": 500.0}, "ratio": {"2025-Q1": 18.7}},
                                    "unit": {"source": "iqvia_nsa", "absolute": {"2025-Q1": 1000.0}, "ratio": {"2025-Q1": 20.5}},
                                },
                            }
                        ],
                        "market_totals": {"activity": {"2025-01": 90.7, "2025-02": 99.3}, "sales": {"2025-Q1": 2675.0}},
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
    "view": "general",
    "source": "ubist",
    "market_id": "M1C",
    "brand": "악템라",
    "dimensions": [
        {
            "dimension_type": "seller",
            "label": "판매사",
            "values": [{"key": "jw중외제약", "value": "JW중외제약", "row_count": 1, "default": False, "selected": False, "flag": True}],
        },
        {
            "dimension_type": "molecule_strength",
            "label": "성분용량",
            "values": [
                {
                    "key": "tocilizumab 162㎎/0.9㎖ [520433BIJ]",
                    "value": "tocilizumab 162㎎/0.9㎖ [520433BIJ]",
                    "row_count": 1,
                    "default": False,
                    "selected": False,
                    "flag": True,
                }
            ],
        }
    ],
    "atc": {
        "atc1": [{"key": "M", "value": "M", "label": "M", "level": "atc1", "parent": None, "default": True, "selected": True, "flag": True}],
        "atc2": [],
        "atc3": [],
        "atc4": [{"key": "M1C", "value": "M1C", "label": "M1C", "level": "atc4", "parent": "M01", "default": True, "selected": True, "flag": True}],
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
    "default_selections": {"atc4": ["M1C"]},
    "applied_selections": {"atc4": ["M1C"]},
    "brand_matched": {
        "atc4": ["M1C"],
        "seller": ["JW중외제약"],
        "form": ["주사제(IJ)"],
        "route": ["주사"],
        "reimbursement": ["급여"],
        "molecule_strength": ["tocilizumab 162㎎/0.9㎖ [520433BIJ]"],
    },
}


FILTER_OPTION_KEY_VALUE_GUIDE: Final = (
    "옵션 항목은 key/value를 모두 가질 수 있습니다. key는 정규화 식별자 또는 UI grouping 보조값이고, "
    "value는 포탈이 선택 상태에 저장해 다음 요청(selections, filters.analysis_level 등)에 다시 넣는 실제 값입니다. "
    "일반 차원 요청에는 value를 다시 넣습니다. 예를 들어 IQVIA molecule_desc 옵션이 "
    "key='carteolol', value='CARTEOLOL'이면 요청에는 value인 'CARTEOLOL'을 보냅니다. "
    "UBIST channel_axis.pairs는 key='종별|진료과' 형태의 조합 식별자이고 value는 facility/specialty 구조를 보여주는 echo입니다."
)


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
                            "description": (
                                "registry 순서의 차원 목록. values의 flag=true는 선택 브랜드 해당 값입니다. "
                                f"{FILTER_OPTION_KEY_VALUE_GUIDE}"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "dimension_type": {"type": "string", "description": "요청 analysis_level에서 쓰는 차원 키"},
                                    "label": {"type": "string", "description": "화면 표시명"},
                                    "values": {
                                        "type": "array",
                                        "description": "선택 가능한 값 목록. 다음 요청에는 각 항목의 value를 사용합니다.",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "key": {
                                                    "type": "string",
                                                    "description": "정규화 식별자 또는 grouping 보조값. 표시/동등성 비교용이며 일반 차원 요청값은 아닙니다.",
                                                },
                                                "value": {
                                                    "type": "string",
                                                    "description": "실제 요청에 다시 넣을 값. 포탈은 이 값을 selections/analysis_level 선택값으로 저장합니다.",
                                                },
                                                "row_count": {"type": "integer", "description": "해당 옵션을 가진 sidecar row 수"},
                                                "default": {"type": "boolean", "description": "초기 선택값이면 true"},
                                                "selected": {"type": "boolean", "description": "현재 selections 입력에 의해 선택됐으면 true"},
                                                "flag": {"type": "boolean", "description": "선택 브랜드 자체가 가진 값이면 true"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                        "atc": {
                            "type": "object",
                            "description": (
                                "ATC1/2/3/4 계층. default/selected/flag 상태 포함. ATC 옵션도 다음 요청에는 value를 사용합니다."
                            ),
                        },
                        "channel_axis": {
                            "type": "object",
                            "description": (
                                "일반뷰 source별 채널 축 registry. UBIST는 facility(종별), specialty(진료과), "
                                "pairs(종별×진료과 조합)를 raw channel_specialty_matrix에서 동적으로 도출하고, "
                                "IQVIA는 audit_code를 raw audit_code_matrix에서 동적으로 도출합니다. "
                                "request에서는 같은 값을 filters.analysis_level.{source} 하위로 접어 보냅니다. "
                                f"{FILTER_OPTION_KEY_VALUE_GUIDE}"
                            ),
                        },
                        "default_selections": {"type": "object", "description": "초기 선택값. 차원 내 값은 OR이며 값은 request-ready value입니다."},
                        "applied_selections": {
                            "type": "object",
                            "description": "현재 selections 입력을 반영한 선택값. 값은 request-ready value입니다.",
                        },
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


AI_ANALYSIS_STAGE_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "bullets": {"type": "array", "items": {}},
        "evidence": {"type": "array", "items": {}},
    },
}


AI_ANALYSIS_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "phenomenon": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "cause": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "prediction": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "recommendation": {"$ref": "#/components/schemas/AIAnalysisStage"},
        "evidence_pool": {"type": "array", "items": {}},
    },
}


AI_ANALYSIS_UNAVAILABLE_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "available": {"type": "boolean", "const": False},
        "reason": {"type": "string"},
    },
}


AI_ANALYSIS_FIELD_SCHEMA: Final = {
    "oneOf": [
        {"$ref": "#/components/schemas/AIAnalysis"},
        {"$ref": "#/components/schemas/AIAnalysisUnavailable"},
    ],
}


BRAND_FACTOR_SOURCE_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "available": {"type": "boolean", "description": "해당 source의 factor 값이 하나 이상 있으면 true입니다."},
        "reason": {
            "type": ["string", "null"],
            "description": "available=false이면 not_generated, true이면 null입니다.",
            "example": "not_generated",
        },
        "values": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
            "description": "source별 catalog factor 값입니다. 다중 값 브랜드는 배열로 반환합니다.",
        },
    },
    "required": ["available", "reason", "values"],
}


UBIST_BRAND_FACTOR_SCHEMA: Final = {
    **deepcopy(BRAND_FACTOR_SOURCE_SCHEMA),
    "properties": {
        **deepcopy(BRAND_FACTOR_SOURCE_SCHEMA["properties"]),
        "values": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "seller": {"type": "array", "items": {"type": "string"}},
                "molecule_strength": {"type": "array", "items": {"type": "string"}},
                "form": {"type": "array", "items": {"type": "string"}},
                "route": {"type": "array", "items": {"type": "string"}},
                "reimbursement": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


IQVIA_BRAND_FACTOR_SCHEMA: Final = {
    **deepcopy(BRAND_FACTOR_SOURCE_SCHEMA),
    "properties": {
        **deepcopy(BRAND_FACTOR_SOURCE_SCHEMA["properties"]),
        "values": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mfr_name_kor": {"type": "array", "items": {"type": "string"}},
                "molecule_type": {"type": "array", "items": {"type": "string"}},
                "molecule_desc": {"type": "array", "items": {"type": "string"}},
                "pack_desc": {"type": "array", "items": {"type": "string"}},
                "strength": {"type": "array", "items": {"type": "string"}},
                "nhi_type": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


SOURCE_BRAND_STRENGTH_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "description": "agent3_brand_strength_source의 소스별 강점 요약입니다.",
    "properties": {
        "profile_display": {"type": "object", "additionalProperties": True},
        "strength_items": {"type": "array", "items": {}},
        "limitations": {"type": "array", "items": {}},
    },
    "required": ["profile_display", "strength_items", "limitations"],
}


def _brand_source_schema(factor_schema: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "소스 데이터가 전혀 없으면 빈 객체입니다.",
        "properties": {
            "factors": deepcopy(factor_schema),
            "strength": deepcopy(SOURCE_BRAND_STRENGTH_SCHEMA),
        },
    }


BRAND_FACTOR_ITEM_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "description": "선택 브랜드 또는 경쟁 브랜드의 소스별 factors+strength 슬롯입니다.",
    "properties": {
        "brand": {"type": "string", "description": "화면 표시 브랜드명입니다."},
        "brand_key": {"type": "string", "description": "mart 기준 브랜드 식별자입니다."},
        "role": {"type": "string", "enum": ["selected", "competitor"], "description": "선택 브랜드인지 경쟁 브랜드인지 구분합니다."},
        "rank": {"type": "integer", "description": "응답 내 순서입니다. selected가 항상 1번입니다."},
        "iqvia": _brand_source_schema(IQVIA_BRAND_FACTOR_SCHEMA),
        "ubist": _brand_source_schema(UBIST_BRAND_FACTOR_SCHEMA),
    },
    "required": ["brand", "brand_key", "role", "rank", "iqvia", "ubist"],
}


DEEP_ANALYSIS_BRAND_FACTORS_SCHEMA: Final = {
    "type": "array",
    "description": "선택 브랜드 1개와 같은 시장 scope의 경쟁 상위 5개 브랜드를 소스별로 묶은 목록입니다.",
    "items": deepcopy(BRAND_FACTOR_ITEM_SCHEMA),
}


DEEP_ANALYSIS_BRAND_FACTORS_EXAMPLE: Final = [
    {
        "brand": "리바로",
        "brand_key": "리바로",
        "role": "selected",
        "rank": 1,
        "iqvia": {
            "factors": {
                "available": True,
                "reason": None,
                "values": {
                    "mfr_name_kor": ["JW중외제약"],
                    "molecule_type": ["SINGLE"],
                    "molecule_desc": ["PITAVASTATIN"],
                    "pack_desc": ["TAB 2MG"],
                    "strength": ["2MG"],
                    "nhi_type": ["급여"],
                },
            },
            "strength": {"profile_display": {"headline": "IQVIA 기준 강점"}, "strength_items": ["시장 내 성장"], "limitations": []},
        },
        "ubist": {
            "factors": {
                "available": True,
                "reason": None,
                "values": {
                    "seller": ["JW중외제약"],
                    "molecule_strength": ["pitavastatin 2mg"],
                    "form": ["정제"],
                    "route": ["내복"],
                    "reimbursement": ["급여"],
                },
            },
            "strength": {"profile_display": {}, "strength_items": [], "limitations": ["strength candidate 0건"]},
        },
    },
    *[
        {
            "brand": brand,
            "brand_key": brand,
            "role": "competitor",
            "rank": rank,
            "iqvia": {},
            "ubist": {},
        }
        for rank, brand in enumerate(["크레스토", "리피토", "로수바미브", "아토젯", "바이토린"], start=2)
    ],
]


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
	                            "additionalProperties": True,
	                            "properties": {
                                "ai_analysis": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                                "ai_analysis_short": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                                "ai_analysis_long": deepcopy(AI_ANALYSIS_FIELD_SCHEMA),
                                "brand_factors": deepcopy(DEEP_ANALYSIS_BRAND_FACTORS_SCHEMA),
                            },
                        },
                    },
                },
                "example": {
                    "brand": "리바로브이",
                    "generated_at": "2026-07-03T01:30:00+09:00",
                    "data": {
                        "ai_analysis": {},
                        "brand_factors": deepcopy(DEEP_ANALYSIS_BRAND_FACTORS_EXAMPLE),
                    },
                },
            }
	        },
	    },
    404: {"description": "브랜드 심층분석 cache 없음"},
}


def _ensure_ai_analysis_components(openapi_schema: dict) -> None:
    components = openapi_schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["AIAnalysisStage"] = deepcopy(AI_ANALYSIS_STAGE_SCHEMA)
    schemas["AIAnalysis"] = deepcopy(AI_ANALYSIS_SCHEMA)
    schemas["AIAnalysisUnavailable"] = deepcopy(AI_ANALYSIS_UNAVAILABLE_SCHEMA)


def build_openapi_schema(app: FastAPI) -> dict:
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        description=app.description,
    )
    _ensure_ai_analysis_components(openapi_schema)
    return openapi_schema


def install_openapi_overrides(app: FastAPI) -> None:
    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = build_openapi_schema(app)
        return app.openapi_schema

    app.openapi = custom_openapi
