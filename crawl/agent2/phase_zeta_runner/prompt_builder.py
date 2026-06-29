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

위 데이터를 활용해서 phenomenon, cause, prediction, recommendation 4단 분석을 한 번에 JSON 으로 생성하세요.
각 stage의 body는 6문장 이상 9문장 이하로 작성하고, 9문장 초과는 금지합니다. bullets는 4개를 유지하세요.
각 stage에는 evidence 배열을 포함하고, stage 본문에 실제로 사용한 bundle 수치 basis 또는 source event 근거를 최소 1개 이상 넣으세요. 근거가 불확실하면 새로 만들지 말고 해당 stage의 확실한 수치 basis를 우선 사용하세요.
prediction stage는 forecast_simulation.available=true이면 1년/3년/5년 전망을 모두 명시하고, bundle의 horizon_1y/horizon_3y/horizon_5y 값을 각각 반영하세요.
95% 신뢰구간 등 CI 수치는 단독 bullet로 분리하지 말고, 해당 예측값과 같은 문장/field에 두어 compact tag(예: ML·UBIST·매출·2029-03)가 동반되게 작성하세요.
prediction evidence는 FULL_BUNDLE_JSON_FOR_FORMATTER 안에 실제로 존재하는 source event 또는 forecast_simulation 수치 basis만 사용하세요. bundle에 없는 근거 제목, news_id, 시뮬레이션명을 새로 만들지 마세요.
prediction stage에서는 forecast_simulation 수치, CI, 불확실성만 서술하세요. 임상/뉴스/급여/허가/출시/경쟁사/약가/정책/제네릭 진입/외부 변수/시장 경쟁 환경 변화/경쟁 심화/시장 변화 모니터링 등 사건성 해석은 cause 또는 recommendation에 두고, prediction에서 그런 표현을 쓰는 경우에는 반드시 bundle의 실제 event를 prediction.evidence에 함께 넣으세요.
prediction body도 예외 없이 6문장 이상 9문장 이하로 작성하세요. forecast 숫자가 많더라도 5문장 이하로 압축하지 말고, 모델/신뢰구간/불확실성 설명을 별도 완결 문장으로 분리하세요.
market_views 또는 forecast_simulation에 UBIST와 IQVIA가 모두 있으면 최종 JSON 전체에서 두 source를 모두 사용하세요. 최소 한 문장 이상에는 (ML·UBIST·...) compact tag를, 최소 한 문장 이상에는 (ML·IQVIA·...) compact tag를 포함해야 하며, 한 source만으로 4단을 작성하지 마세요.
forecast_simulation 수치는 bundle의 raw value와 단위를 그대로 사용하세요. 원 단위를 억/만/k/M 등으로 환산하거나 IQVIA 수량 단위를 임의로 KRW처럼 바꾸지 마세요.
bundle 수치가 음수인 qoq_pct/yoy_pct/growth 계열 비율은 출력에서도 반드시 '-' 부호를 보존하세요. 감소/하락 문맥이라도 양수처럼 쓰지 말고, 부호가 확실하지 않으면 그 수치를 사용하지 마세요.
prediction.evidence의 numeric basis는 본문에 쓴 forecast_simulation 또는 market_views의 실제 수치를 그대로 복사한 경우에만 넣으세요. 정확한 bundle 수치 basis를 못 고르면 generic '수치 근거'나 가공 숫자를 만들지 말고 evidence를 빈 배열로 두세요.
"""
