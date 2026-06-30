from __future__ import annotations

import json
from typing import Any

from .config import RunnerConfig


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _competitor_event_count(competitor_events: dict[str, Any]) -> int:
    by_view = competitor_events.get("by_view") or {}
    if by_view:
        return sum(
            len(comp.get("events", []) or [])
            for view_payload in by_view.values()
            for comp in view_payload.get("competitors", []) or []
        )
    return sum(
        len(comp.get("events", []) or [])
        for source_payload in (competitor_events.get("by_source") or {}).values()
        for comp in source_payload.get("competitors", []) or []
    )


def _variant_instruction(analysis_variant: str) -> str:
    if analysis_variant == "short":
        return """
[analysis_variant: short — 단기 인사이트]
이번 출력은 단기 인사이트입니다. forecast_simulation에서는 horizon_1y를 prediction의 주된 근거로 사용하세요.
1년 내 변화, 최근 이벤트, 가까운 처방/경쟁 대응, 즉시 대응해야 할 실행 신호를 중심으로 4단을 작성하세요.
recommendation은 다음 1~4개 분기 안에 실행 가능한 현장 메시지, 타깃 세그먼트, 모니터링 액션으로 제한하세요.
prediction은 horizon_1y 수치와 1년 신뢰구간만 사용하고, 3년/5년 예측값이나 장기 구조 전망 수치를 쓰지 마세요.
5년 구조 변화나 장기 포지셔닝을 prediction/recommendation의 주된 근거로 삼지 마세요.
"""
    if analysis_variant == "long":
        return """
[analysis_variant: long — 장기 인사이트]
이번 출력은 장기 인사이트입니다. forecast_simulation에서는 horizon_5y를 prediction의 주된 근거로 사용하세요.
5년 구조적 추세, 지속 성장/둔화, 시장 구조 변화, 경쟁 포지션, 전략 포지셔닝과 CI 폭의 장기 리스크를 중심으로 4단을 작성하세요.
horizon_3y는 5년 전망으로 가는 중간 점검점으로만 사용하고, 단기 실행 체크리스트로 축소하지 마세요.
5년 horizon_5y base가 0이거나 CI 하한이 0인 source/measure가 있으면 그 값을 임의 단위로 바꾸지 말고, 같은 brand bundle 안의 다른 source/measure에서 실제로 존재하는 non-zero horizon_5y 값을 우선 사용하세요. 0을 써야 한다면 forecast_simulation에 있는 source/measure/period compact tag를 정확히 그대로 붙이고, counting_unit/unit/dosage_unit을 서로 바꾸지 마세요.
recommendation은 3~5년 관점의 포트폴리오, 메시지 자산, 투자/방어 전략으로 작성하세요.
"""
    return """
[analysis_variant: legacy]
이번 출력은 기존 운영 호환 인사이트입니다. prediction stage에 1년/3년/5년 전망을 모두 포함하세요.
"""


def build_question_string(bundle: dict[str, Any], config: RunnerConfig | None = None) -> str:
    """Build the single `question` string consumed by GenOS workflow 217."""

    brand_context = bundle.get("brand_context", {}) or {}
    event_bundle = bundle.get("event_bundle", {}) or {}
    forecast = bundle.get("forecast_simulation", {}) or {}
    competitor_events = bundle.get("competitor_events", {}) or {}

    brand_name = bundle.get("bundle_meta", {}).get("brand") or brand_context.get("name")
    snapshot_at = bundle.get("bundle_meta", {}).get("snapshot_at", "")
    events_brand_centric = event_bundle.get("events_brand_centric", []) or []
    events_market_trend = event_bundle.get("events_market_trend", []) or []
    cross_match_events = event_bundle.get("cross_match_events", []) or []
    runner_config = config.config_version if config else "phase_zeta_runner_genos_v1"
    analysis_variant = config.analysis_variant if config else "legacy"

    return f"""[분석 대상]
brand: {brand_name}
snapshot: {snapshot_at}
mkt_team: {brand_context.get("mkt_team")}
runner_config: {runner_config}

[brand 메타]
{_dump(brand_context)}

[시장 view 데이터 — 총 {len(bundle.get("market_views", []) or [])} view]
{_dump(bundle.get("market_views", []) or [])}

[brand 직접 events — {len(events_brand_centric)} 건]
{_dump(events_brand_centric)}

[시장 동향 events — {len(events_market_trend)} 건]
{_dump(events_market_trend)}

[cross_match events — {len(cross_match_events)} 건]
{_dump(cross_match_events)}

[경쟁사 events (view/source 별) — {_competitor_event_count(competitor_events)} 건]
{_dump(competitor_events)}

[forecast/simulation]
available: {bool(forecast.get("available", False))}
{_dump(forecast)}

[FULL_BUNDLE_JSON_FOR_FORMATTER]
{_dump(bundle)}
[/FULL_BUNDLE_JSON_FOR_FORMATTER]

{_variant_instruction(analysis_variant)}

위 데이터를 활용해서 phenomenon, cause, prediction, recommendation 4단 분석을 한 번에 JSON 으로 생성하세요.
각 stage의 body는 6문장 이상 9문장 이하로 작성하고, 9문장 초과는 금지합니다. bullets는 4개를 유지하세요.
phenomenon/cause/prediction/recommendation 모두 body 6~9문장을 동일하게 지키세요. 특히 recommendation도 bullet 요약으로 압축하지 말고 6문장 이상의 완결된 실행 설명을 작성하세요.
recommendation body도 formatter가 문장으로 인식할 수 있게 각 문장을 "...해야 한다.", "...필요가 있다.", "...수립한다."처럼 평서형/당위형으로 끝내세요. "하십시오", "강화하십시오" 같은 명령형 종결은 문장 수를 깨뜨릴 수 있으므로 쓰지 마세요.
각 stage에는 evidence 배열을 포함하고, stage 본문에 실제로 사용한 bundle 수치 basis 또는 source event 근거를 최소 1개 이상 넣으세요. 근거가 불확실하면 새로 만들지 말고 해당 stage의 확실한 수치 basis를 우선 사용하세요.
prediction stage는 analysis_variant 지시를 우선 따르세요. legacy는 1년/3년/5년 전망을 모두 명시하고 bundle의 horizon_1y/horizon_3y/horizon_5y 값을 각각 반영하며, short는 horizon_1y 중심, long은 horizon_5y 중심으로 작성하세요.
rank는 FULL_BUNDLE_JSON_FOR_FORMATTER에 실제 rank 수치가 있을 때만 쓰세요. 100위권 밖, 200위권 밖처럼 임의의 숫자를 붙인 순위권 표현은 쓰지 말고, 필요한 경우 숫자 없이 순위권 밖이라고만 서술하세요.
95% 신뢰구간 등 CI 수치는 단독 bullet로 분리하지 말고, 해당 예측값과 같은 문장/field에 두어 compact tag(예: ML·UBIST·매출·2029-03)가 동반되게 작성하세요.
prediction evidence는 FULL_BUNDLE_JSON_FOR_FORMATTER 안에 실제로 존재하는 source event 또는 forecast_simulation 수치 basis만 사용하세요. bundle에 없는 근거 제목, news_id, 시뮬레이션명을 새로 만들지 마세요.
prediction stage에서는 forecast_simulation 수치와 95% CI를 유지하되, 1년/3년/5년 horizon의 방향성(성장/둔화/정체/감소), CI 폭 변화와 장단기 불확실성, 현재 시장/처방 지표와 연결한 시사점을 함께 서술하세요. prediction은 미래 전망의 해석이며 cause의 과거 원인 분석이나 recommendation의 실행 지시와 중복하지 마세요. 임상/뉴스/급여/허가/출시/경쟁사/약가/정책/제네릭 진입/외부 변수/시장 경쟁 환경 변화/경쟁 심화/시장 변화 모니터링 등 사건성 해석은 bundle event가 있을 때만 prediction.evidence에 연결하고, bundle event가 없으면 prediction에서 단정하지 마세요.
prediction body에는 숫자/CI를 나열하는 문장과 별도로, forecast 수치가 의미하는 시장/처방 방향성·CI 폭의 리스크·현재 지표와 연결한 시사점 해석 문장을 최소 3개 포함하세요. 단, bundle event가 없는 정책/경쟁/급여/허가/출시 같은 외부 변수 가정으로 해석문을 채우지 말고 forecast와 market_views 수치 자체의 의미를 해석하세요.
prediction body도 예외 없이 6문장 이상 9문장 이하로 작성하세요. forecast 숫자가 많더라도 5문장 이하로 압축하지 말고, 모델/신뢰구간/불확실성 설명을 별도 완결 문장으로 분리하세요.
market_views 또는 forecast_simulation에 UBIST와 IQVIA가 모두 있으면 최종 JSON 전체에서 두 source를 모두 사용하세요. 최소 한 문장 이상에는 (ML·UBIST·...) compact tag를, 최소 한 문장 이상에는 (ML·IQVIA·...) compact tag를 포함해야 하며, 한 source만으로 4단을 작성하지 마세요.
forecast_simulation 수치는 bundle의 raw value와 단위를 그대로 사용하세요. 원 단위를 억/만/k/M 등으로 환산하거나 IQVIA 수량 단위를 임의로 KRW처럼 바꾸지 마세요.
수량/매출 숫자는 bundle의 원 숫자 표기를 그대로 쓰세요. "15만 3,841", "1.2억", "47.7만"처럼 한국식 혼합 단위로 다시 쓰지 말고, compact tag 앞 숫자는 bundle에 있는 전체 숫자 하나만 남기세요.
bundle 수치가 음수인 qoq_pct/yoy_pct/growth 계열 비율은 출력에서도 반드시 '-' 부호를 보존하세요. 감소/하락 문맥이라도 양수처럼 쓰지 말고, 부호가 확실하지 않으면 그 수치를 사용하지 마세요.
전월 대비, 전년 대비, YoY, MoM, qoq_pct, yoy_pct, mom_pct, growth 계열 비율은 FULL_BUNDLE_JSON_FOR_FORMATTER에 실제 수치가 있을 때만 사용하세요. 앞뒤 월 매출/처방량으로 직접 계산하거나, 유사 브랜드/다른 기간의 비율을 가져오거나, "변동폭" 같은 표현으로 추정치를 만들지 마세요. 정확한 tagged percentage를 찾지 못하면 해당 비율 문장을 쓰지 말고 절대 새 퍼센트를 생성하지 마세요.
"최근 월간 매출 흐름에서 15.53% 변동이 관찰"처럼 bundle에 없는 퍼센트를 "흐름/변동/증감/성장" 문맥으로 포장해 쓰지 마세요. bundle 안의 정확한 tagged percentage가 없으면 raw 매출·수량·MS 수치만 쓰고, 변화율은 숫자 없이 정성적으로만 서술하세요.
compact tag는 반드시 (ML·IQVIA·measure·period), (ML·UBIST·measure·period), (CD·IQVIA·measure·period), (CD·UBIST·measure·period) 형식의 bundle 수치에만 붙이세요. 정책/뉴스/약가/급여/허가/임상 같은 사건성 근거에는 (ML·정책·약가인상) 같은 임의 compact tag를 만들지 말고 source event evidence로만 연결하세요.
prediction.evidence의 numeric basis는 본문에 쓴 forecast_simulation 또는 market_views의 실제 수치를 그대로 복사한 경우에만 넣으세요. 정확한 bundle 수치 basis를 못 고르면 generic '수치 근거'나 가공 숫자를 만들지 말고 evidence를 빈 배열로 두세요.
"""
