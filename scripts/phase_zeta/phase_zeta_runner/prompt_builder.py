from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from .config import RunnerConfig


SYSTEM_INSTRUCTION = """당신은 JW중외제약 마케팅팀을 위한 시장 분석 AI 입니다.
선택된 brand 의 4단 분석 (현상/원인/예측/권고) 을 한 번에 생성합니다.

[★ 절대 규칙 — 위반 금지]
1. 모든 수치는 입력된 bundle 데이터에서 그대로 인용. 새 계산/추정 절대 금지.
2. 수치 표기: comma-separated raw value (예: "11,687,229,691.75 KRW").
3. 만/억/k/M 단위 변환 절대 금지.
4. 비율 (M/S 등) 은 부호 없이 표기: "4.13%".
5. 변동률 (MoM/YoY/MAT YoY/CAGR 등) 은 부호 포함 표기: "+34.87%" 또는 "-21.44%".
6. EI, Momentum, rank 등 KPI 도 bundle 의 값 그대로 인용.
7. forecast_simulation.available=false 시 forecast 데이터 인용 금지.
8. 응답은 자연스러운 한국어로 작성하고 외국어 직역체를 피합니다.

[4단 분석 구조]
- phenomenon (현상): brand 의 현재 시장 상황. bundle 의 market_views, events_brand_centric 활용.
- cause (원인): phenomenon 의 trend 의 원인. events_market_trend, cross_match_events, competitor_events 활용.
- prediction (예측): short-term 예측. forecast 비활성 시 events trend + 최근 metric 만 사용.
- recommendation (권고): JW 마케팅 팀의 actionable 권고. 위 3단의 evidence 만 종합. 새 데이터 도입 X.

[각 stage 의 출력 형식]
- title: 한 줄 헤드라인 (★ 핵심 수치 1개 포함 권장)
- body: 2-3 문장 (★ stage 의 분석 내용)
- bullets: 2-3개, 각 bullet 에 수치 1+ 인용 권장

[bundle 활용 가이드]
- bundle.brand_context: brand 메타 (★ molecule, atc4, sources, mkt_team)
- bundle.market_views: 시장 view 별 metric (★ 최대 12 view)
- bundle.event_bundle.events_brand_centric: phenomenon 의 핵심 input (★ brand 직접 언급)
- bundle.event_bundle.events_market_trend: cause 의 input (★ 시장 동향)
- bundle.event_bundle.cross_match_events: cause 의 보조 input (★ JW brand 의 mirror)
- bundle.competitor_events.by_source: cause 의 보조 input (★ source 별 시장 top5)
- bundle.forecast_simulation: prediction 의 input (★ 현재 available=false)

[reason 필드 활용]
- 각 event 의 reason 필드는 workflow 196 LLM 의 분석 결과 (★ score 의 근거).
- 분석 시 reason 의 내용을 참고하되, 그대로 인용 X. 본 LLM 의 분석으로 재해석.
"""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "phenomenon": {
            "type": "OBJECT",
            "description": "현재 brand 의 시장 상황 분석",
            "properties": {
                "title": {"type": "STRING", "description": "한 줄 헤드라인 (★ 80자 이내)"},
                "body": {"type": "STRING", "description": "2-3 문장 본문"},
                "bullets": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "minItems": 2,
                    "maxItems": 3,
                    "description": "각 bullet 1+ 수치 인용 권장",
                },
            },
            "required": ["title", "body", "bullets"],
        },
        "cause": {
            "type": "OBJECT",
            "description": "phenomenon trend 의 원인 분석",
            "properties": {
                "title": {"type": "STRING"},
                "body": {"type": "STRING"},
                "bullets": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": 2, "maxItems": 3},
            },
            "required": ["title", "body", "bullets"],
        },
        "prediction": {
            "type": "OBJECT",
            "description": "단기 예측",
            "properties": {
                "title": {"type": "STRING"},
                "body": {"type": "STRING"},
                "bullets": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": 2, "maxItems": 3},
            },
            "required": ["title", "body", "bullets"],
        },
        "recommendation": {
            "type": "OBJECT",
            "description": "JW 마케팅팀 actionable 권고",
            "properties": {
                "title": {"type": "STRING"},
                "body": {"type": "STRING"},
                "bullets": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": 2, "maxItems": 3},
            },
            "required": ["title", "body", "bullets"],
        },
    },
    "required": ["phenomenon", "cause", "prediction", "recommendation"],
}


@dataclass(frozen=True)
class UnifiedPrompt:
    system_instruction: str
    user_message: str
    response_schema: dict[str, Any]


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def build_unified_prompt(bundle: dict, config: RunnerConfig) -> UnifiedPrompt:
    brand_context = bundle.get("brand_context", {})
    event_bundle = bundle.get("event_bundle", {})
    forecast = bundle.get("forecast_simulation", {})
    brand_name = bundle.get("bundle_meta", {}).get("brand") or brand_context.get("name")
    snapshot_at = bundle.get("bundle_meta", {}).get("snapshot_at", "")

    events_brand_centric = event_bundle.get("events_brand_centric", [])
    events_market_trend = event_bundle.get("events_market_trend", [])
    cross_match_events = event_bundle.get("cross_match_events", [])
    competitor_events = bundle.get("competitor_events", {})
    competitor_event_count = sum(
        len(comp.get("events", []))
        for source_payload in (competitor_events.get("by_source") or {}).values()
        for comp in source_payload.get("competitors", [])
    )

    user_message = f"""[분석 대상]
brand: {brand_name}
snapshot: {snapshot_at}
mkt_team: {brand_context.get("mkt_team")}
runner_config: {config.config_version}

[brand 메타]
{_dump(brand_context)}

[시장 view 데이터 — 총 {len(bundle.get("market_views", []))} view]
{_dump(bundle.get("market_views", []))}

[brand 직접 events — {len(events_brand_centric)} 건]
{_dump(events_brand_centric)}

[시장 동향 events — {len(events_market_trend)} 건]
{_dump(events_market_trend)}

[cross_match events — {len(cross_match_events)} 건]
{_dump(cross_match_events)}

[경쟁사 events (source 별) — {competitor_event_count} 건]
{_dump(competitor_events)}

[forecast/simulation]
available: {bool(forecast.get("available", False))}
{_dump(forecast)}

위 데이터를 활용해서 phenomenon, cause, prediction, recommendation 4단 분석을 한 번에 생성하세요.
"""
    return UnifiedPrompt(
        system_instruction=SYSTEM_INSTRUCTION,
        user_message=user_message,
        response_schema=copy.deepcopy(RESPONSE_SCHEMA),
    )
