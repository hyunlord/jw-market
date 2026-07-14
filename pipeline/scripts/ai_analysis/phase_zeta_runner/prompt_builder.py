from __future__ import annotations

import json
from typing import Any

from .config import RunnerConfig, require_analysis_variant

PROCESSING_MODE_FULL = "full"
PROCESSING_MODE_COMPACT = "compact"
PROCESSING_MODE_RECAP = "recap"


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


def _forecast_has_all_horizons(forecast: dict[str, Any]) -> bool:
    if not forecast.get("available"):
        return False
    by_view = forecast.get("by_view")
    if not isinstance(by_view, dict):
        return False
    required = {"horizon_1y", "horizon_3y", "horizon_5y"}
    return any(isinstance(payload, dict) and required.issubset(payload) for payload in by_view.values())


def _mode_instruction(mode: str) -> str:
    match mode:
        case "compact":
            return "compact 모드: 동일한 4단 구조를 유지하되 각 단락 body는 간결하게 쓰고 bullets는 2-4개만 작성하세요."
        case "recap":
            return "recap 모드: 동일한 4단 구조를 유지하되 각 단락 body는 1-2문장으로 요약하고 bullets는 2개만 작성하세요."
        case _:
            return ""


def _variant_instruction(analysis_variant: str) -> str:
    variant = require_analysis_variant(analysis_variant)
    if variant == "short":
        return (
            "short variant: 단기 인사이트입니다. forecast_simulation에서는 horizon_1y를 prediction의 주된 근거로 사용하세요. "
            "1년 내 변화, 최근 이벤트, 가까운 처방/경쟁 대응, 즉시 실행 신호를 중심으로 쓰세요. "
            "prediction은 horizon_1y 수치와 1년 신뢰구간만 사용하고, 3년/5년 예측값이나 장기 구조 전망 수치를 쓰지 마세요."
        )
    if variant == "long":
        return (
            "long variant: 장기 인사이트입니다. forecast_simulation에서는 horizon_5y를 prediction의 주된 근거로 사용하세요. "
            "5년 구조적 추세, 지속 성장/둔화, 시장 구조 변화, 경쟁 포지션, 전략 포지셔닝과 CI 폭의 장기 리스크를 중심으로 쓰세요. "
            "horizon_3y는 5년 전망으로 가는 중간 점검점으로만 사용하고, recommendation은 3~5년 관점으로 작성하세요. "
            "단, horizon_5y가 제공되지 않은 브랜드에서는 5년 수치를 만들지 말고, 제공된 가장 긴 horizon 또는 market/competitive evidence로 장기 방향성을 서술하세요."
        )
    return "legacy variant: 기존 운영 호환 인사이트입니다. prediction stage에 1년/3년/5년 전망을 모두 포함하세요."


def _validation_contract_block(
    forecast: dict[str, Any],
    analysis_variant: str = "legacy",
    market_views: list[dict[str, Any]] | None = None,
) -> str:
    variant = require_analysis_variant(analysis_variant)
    horizon_rule = ""
    if _forecast_has_all_horizons(forecast):
        if variant == "short":
            horizon_rule = (
                "\n- short variant에서는 forecast_simulation.available=true일 때 prediction stage에 horizon_1y 실제 수치를 명시하세요. "
                "3y/5y 값은 단기 판단의 주 근거로 쓰지 마세요."
            )
        elif variant == "long":
            horizon_rule = (
                "\n- long variant에서는 forecast_simulation.available=true일 때 prediction stage에 horizon_5y 실제 수치를 명시하세요. "
                "horizon_3y는 5y 전망의 중간 점검점으로만 사용하세요."
            )
        else:
            horizon_rule = (
                "\n- forecast_simulation.available=true이고 1y/3y/5y 값이 모두 제공된 경우, "
                "prediction stage에서 시뮬레이션을 언급할 때는 1y/3y/5y 각 horizon의 실제 수치를 모두 명시하세요. "
                "방향성 서술만 쓰지 마세요."
            )
    has_general = any(view.get("view") == "general_view" for view in market_views or [])
    source_contract = (
        "`General View · {SOURCE} 기준 (ATC4)` 형식의 전체 표기"
        if has_general
        else "`Market Landscape · {SOURCE} 기준` 또는 `Competitive Dynamics · {SOURCE} 기준` 형식의 전체 표기"
    )
    forbidden_short = "`GENERAL·UBIST·매출`" if has_general else "`ML·UBIST·매출`, `CD·IQVIA·매출`"
    return (
        "\n\n[검증 계약]\n"
        f"- market/competitive 수치를 인용할 때는 반드시 {source_contract}를 함께 쓰세요. "
        f"{forbidden_short} 같은 축약형만 쓰는 것은 금지입니다.\n"
        "- prediction evidence의 event/news 근거는 반드시 위 retained event 목록의 `news_id` 또는 `title`을 정확히 복사해서 쓰세요. "
        "retained event 목록에 없는 사건은 인용하지 마세요.\n"
        "- retained event나 실제 수치/시뮬레이션 근거가 마땅치 않으면 prediction evidence 배열을 비워두거나 항목 수를 줄이세요. "
        "source label만 있는 근거, 예측 데이터 부재 placeholder, 실제 뉴스 제목/수치가 없는 근거를 억지로 만들지 마세요.\n"
        "- 수치/시뮬레이션 근거는 `basis`에 실제 수치와 source/view 표기를 함께 넣어 event/news 근거와 구분하세요."
        "\n- bundle에 있는 수치만 인용하고, bundle 밖의 수치를 계산하거나 추정하지 마세요. "
        "forecast_simulation의 KRW 값은 원문 숫자 그대로 쓰며 억/만 단위로 변환하지 마세요."
        f"{horizon_rule}"
    )


def build_question_string(bundle: dict[str, Any], config: RunnerConfig | None = None, mode: str = PROCESSING_MODE_FULL) -> str:
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

    mode_instruction = _mode_instruction(mode)
    mode_block = f"\n\n[출력 밀도]\n{mode_instruction}" if mode_instruction else ""
    variant_block = f"\n\n[analysis_variant: {analysis_variant}]\n{_variant_instruction(analysis_variant)}"
    validation_contract = _validation_contract_block(forecast, analysis_variant, bundle.get("market_views") or [])

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

	위 데이터를 활용해서 phenomenon, cause, prediction, recommendation 4단 분석을 한 번에 JSON 으로 생성하세요.{validation_contract}{mode_block}{variant_block}
	"""
