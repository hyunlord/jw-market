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

### 요청 body 최상위 필드

| 필드 | 타입 | 필수 | 기본값 | missing 처리 | null 처리 |
|---|---|---:|---|---|---|
| `source` | string | 아니오 | `ubist` | `ubist`로 계산 | 422 validation error |
| `measure` | string | 아니오 | `sales` | `sales`로 계산 | 422 validation error |
| `filters` | object | 아니오 | 빈 필터 객체 | 빈 필터 객체 | 422 validation error |
| `options` | object | 아니오 | `{top_n:20, metrics:[], period_range:null}` | 기본 옵션 객체 | 422 validation error |

`source`는 `ubist`, `iqvia`, `iqvia_nsa`, `nsa`를 받을 수 있고 내부에서는 `iqvia`/`nsa`가
`iqvia_nsa`로 정규화됩니다. `measure`는 UBIST에서 `sales`, `volume`, IQVIA에서
`sales`, `unit`, `counting_unit`, `dosage_unit`만 유효합니다.

### `filters` 필드

| 필드 | 타입 | 기본값 | 동작 |
|---|---|---|---|
| `atc4` | string[] | `[]` | 일반뷰 범위. 공백 제거 후 대문자 dedupe. 일반뷰는 `focus_brand_key`로 단일 ATC4를 추론하지 못하면 `atc4`가 필요합니다. 전략뷰에서는 보내면 400입니다. |
| `molecule` | string[] | `[]` | 모델 필드는 있으나 현재 D-1 동적 필터에서는 비활성입니다. 값이 있으면 400입니다. |
| `view_kind` | string/null | null | `market_landscape`/`strategic_ml`/`ml`은 ML 전략뷰, `competitive_dynamics`/`strategic_cd`/`cd`는 CD 전략뷰입니다. 값이 있으면 전략뷰 분기로 들어갑니다. |
| `ml_id` | string/null | null | ML 전략 시장 id입니다. `focus_brand_key`와 ML view를 함께 보내면 브랜드 catalog의 대표 `ml_id`가 우선될 수 있습니다. |
| `cd_market_id` | string/null | null | CD 전략 시장 id입니다. 있으면 CD 전략뷰로 계산합니다. |
| `focus_brand_key` | string/null | null | 브랜드 기준 기본 ATC4/시장 해석에 사용합니다. 빈 문자열은 대부분 미입력처럼 처리됩니다. |
| `analysis_level` | object | 빈 source 객체 | 소스별 분석레벨 필터입니다. 차원 내 값은 OR, 서로 다른 차원은 AND로 적용됩니다. |
| `channel_axis` | object | 빈 source 객체 | 일반뷰 채널축 필터입니다. 전략뷰에서는 active 값이 있으면 400입니다. |

`filters` 자체를 생략하면 빈 객체로 처리됩니다. `filters:null`은 허용되지 않습니다.
중첩 list 필드는 생략하면 `[]`, `null`이면 422, 빈 list이면 적용하지 않습니다.
선택 string 필드(`view_kind`, `ml_id`, `cd_market_id`, `focus_brand_key`)는 missing과 null이 모두 `None`이며,
빈 문자열은 resolver의 truthy/strip 조건에 따라 미입력 또는 잘못된 id로 처리될 수 있으므로 보내지 않는 것을 권장합니다.

### 일반뷰 `analysis_level` 허용 키

UBIST는 `analysis_level.ubist` 안에서 `atc3`, `atc4`, `seller`, `molecule_strength`,
`form`, `route`, `reimbursement`를 적용할 수 있습니다. 모델에는 `class`, `molecule`,
`strength_pack`, `ox_gx`도 보이지만 현재 resolver 매핑/registry에서는 동적 필터로 쓰지 않으며,
값을 넣으면 unsupported/disabled 400이 날 수 있습니다.

IQVIA는 `analysis_level.iqvia` 안에서 `mfr_name_kor`, `molecule_type`, `molecule_desc`,
`pack_desc`, `strength`, `nhi_type`를 적용합니다. `pack_desc`는 canonical sidecar의
`dimension_type='pack'` 행을 조회해 PACK DESC 텍스트 단위로 제품 범위를 좁힙니다.
`mfr`, `nhi`, `audit_code`는 모델 필드가 있어도
현재 resolver 매핑에는 없으므로 적용 필터로 보내지 마십시오.

다른 source의 객체에 값이 있으면 400입니다. 예를 들어 `source:"iqvia"` 요청에서
`analysis_level.ubist.seller`에 값이 있으면 `analysis_level must match selected source`가 반환됩니다.

### `channel_axis`

UBIST 일반뷰: `channel_axis.ubist.facility`, `specialty`, `pairs[{facility,specialty}]`를 지원합니다.
IQVIA 일반뷰: `channel_axis.iqvia.audit_code`를 지원하며 값은 strip 후 대문자로 정규화됩니다.
선택한 `source`와 다른 channel axis에 값이 있으면 400입니다. 전략뷰에서는 active channel axis 자체가 400입니다.

### `options`

`top_n`은 기본 20이고 1~100 범위입니다. `top_n:null`은 런타임에서 20으로 보정됩니다.
`metrics`는 예약 필드이며 현재 계산 로직은 사용하지 않습니다. `period_range.start/end`는 선택 기간 경계입니다.
`period_range`를 생략하거나 null이면 전체 기간을 사용합니다. `period_range:{}`는 시작/끝 모두 없는 전체 기간과 같습니다.

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

`/api/brand-activity/*`도 `filters.atc4`, `filters.analysis_level`, `filters.channel_axis`라는 같은 시장 필터 개념을 씁니다.
다만 같은 Pydantic 클래스를 공유하지는 않습니다. Dynamic-Market은 알 수 없는 필드를 `extra=forbid`로 거절하지만,
Brand-Activity는 중첩 필터 모델이 extra 값을 허용합니다. 실제 Brand-Activity service handler는 일반뷰 시장 id를
flat `filters.atc4`에서 읽으므로, Pydantic 모델에 보이는 nested `filters.atc.atc4`만 보내면 400
(`filters.atc4 and selected_brand are required`)이 날 수 있습니다. Brand-Activity에서는 `filters`가 비어 있으면
legacy `filter`를 대신 쓰며, 둘 다 비어 있으면 빈 필터로 처리됩니다.
"""


DYNAMIC_MARKET_REQUEST_BODY_DESCRIPTION: Final = (
    "동적 원인분석 요청입니다. 필드별 missing/null/빈값 처리와 source별 허용 필터는 endpoint 설명을 참조하십시오."
)


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
            }
        },
        "channel_axis": {"ubist": {"facility": ["의원"], "specialty": ["내분비"]}},
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
                "strength": ["162MG"],
                "nhi_type": ["NHI"],
            }
        },
        "channel_axis": {"iqvia": {"audit_code": ["KHPA", "KPA"]}},
    },
    "options": {"top_n": 10, "period_range": {"start": "2024-Q1", "end": "2026-Q1"}},
}


COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE: Final = {
    "source": "ubist",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "리바로",
        "cd_market_id": "cd_001",
        "view_kind": "competitive_dynamics",
    },
    "options": {"top_n": 20},
}


GENERAL_IQVIA_PACK_DESC_FILTER_REQUEST_EXAMPLE: Final = {
    "source": "iqvia",
    "measure": "sales",
    "filters": {
        "focus_brand_key": "악템라",
        "atc4": ["M01C0"],
        "analysis_level": {"iqvia": {"pack_desc": ["PFS 162MG/0.9ML"]}},
    },
    "options": {"top_n": 20},
}


DYNAMIC_MARKET_REQUEST_EXAMPLES: Final = {
    "general_baseline": {
        "summary": "일반뷰 기본 조회: ATC4만 지정",
        "description": "UBIST 일반뷰에서 필터 없이 ATC4 범위만 계산합니다.",
        "value": GENERAL_BASELINE_REQUEST_EXAMPLE,
    },
    "general_ubist_filters": {
        "summary": "일반뷰 UBIST 분석레벨+채널축",
        "description": "UBIST에서 seller/form/route/reimbursement 등 UBIST 전용 필터와 specialty 채널축을 함께 적용합니다.",
        "value": GENERAL_UBIST_FILTER_REQUEST_EXAMPLE,
    },
    "general_iqvia_filters": {
        "summary": "일반뷰 IQVIA 분석레벨+audit_code",
        "description": "IQVIA는 mfr_name_kor/molecule_desc/strength/nhi_type과 audit_code 채널축을 사용합니다.",
        "value": GENERAL_IQVIA_FILTER_REQUEST_EXAMPLE,
    },
    "market_landscape": {"summary": "전략뷰 Market Landscape: ml_id", "value": DYNAMIC_MARKET_REQUEST_EXAMPLE},
    "competitive_dynamics": {
        "summary": "전략뷰 Competitive Dynamics: cd_market_id",
        "value": COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE,
    },
    "general_iqvia_pack_desc_filter": {
        "summary": "일반뷰 IQVIA PACK DESC 필터",
        "description": "`analysis_level.iqvia.pack_desc`는 PACK DESC 텍스트를 `dimension_type=pack`으로 매핑해 필터링합니다.",
        "value": GENERAL_IQVIA_PACK_DESC_FILTER_REQUEST_EXAMPLE,
    },
}


DYNAMIC_MARKET_ERROR_EXAMPLES: Final = {
    "unsupported_filter_key": {
        "summary": "지원하지 않는 필터 키",
        "value": {
            "detail": {
                "error": "invalid_dynamic_market_request",
                "message": "unsupported analysis_level dimension for iqvia_nsa: audit_code",
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
    "atc4": ["C10A1"],
    "analysis_level": {
        "ubist": {
            "seller": ["JW중외제약"],
            "molecule_strength": ["pitavastatin calcium 2mg [470901ATB]"],
        },
        "iqvia": {
            "mfr_name_kor": ["제이더블유중외제약"],
            "molecule_type": ["SINGLE"],
            "molecule_desc": ["PITAVASTATIN"],
            "strength": ["2MG"],
            "nhi_type": ["NHI"],
        },
    },
    "channel": {
        "visit_location": ["의원"],
        "specialty": ["순환기(Cardiology IM)"],
        "audit_code": ["KPA"],
    },
    "channel_axis": {
        "ubist": {"facility": ["의원"], "specialty": ["순환기(Cardiology IM)"]},
        "iqvia": {"audit_code": ["KPA", "KHPA"]},
    },
}


BRAND_ACTIVITY_FILTER_DESCRIPTION: Final = """
Brand-Activity 계열은 Dynamic-Market과 같은 시장 필터 개념을 공유하지만 request model은 별도입니다.

| 구분 | Dynamic-Market | Brand-Activity |
|---|---|---|
| ATC4 위치 | `filters.atc4` | `filters.atc4` |
| source 위치 | 최상위 `source` 필수/기본값 | endpoint/service가 선택 브랜드와 필터에서 해석 |
| 분석레벨 위치 | `filters.analysis_level.ubist/iqvia` | `filters.analysis_level.ubist/iqvia` |
| 채널축 위치 | `filters.channel_axis` | `filters.channel_axis` 또는 top-level `channel_axis` |
| unknown field | top-level/nested 대부분 거절(`extra=forbid`) | top-level은 ignore, nested filter는 allow |
| legacy 필터 | 없음 | `filters`가 비면 `filter`를 대신 사용 |

Brand-Activity의 `filters:null`/`filter:null`은 validation error입니다. 생략하면 빈 필터 객체입니다.
`filters`와 `filter`를 둘 다 보내면 비어 있지 않은 `filters`가 우선합니다. 일반뷰 handler는
flat `filters.atc4`를 시장 id로 사용합니다. `filters.atc.atc4`는 모델에 보이는 nested 호환 필드이지만
현재 service parser의 필수 ATC4 판정에는 쓰이지 않습니다. top-level `channel_axis`는 `filters.channel_axis`가 없을 때만 병합됩니다.
"""


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
    "channel_axis": {"iqvia": {"audit_code": ["KPA"]}},
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
                            },
                        },
                    },
                },
                "example": {"brand": "리바로", "generated_at": "2026-07-03T01:30:00+09:00", "data": {"ai_analysis": {}}},
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
