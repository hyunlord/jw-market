from __future__ import annotations
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class AnswerIntent(StrEnum):
    MARKET_SIZE_TREND = "MARKET_SIZE_TREND"
    BRAND_TREND = "BRAND_TREND"
    MARKET_OUTLOOK = "MARKET_OUTLOOK"
    COMPETITION_CHANGE = "COMPETITION_CHANGE"
    COMPETITOR_POSITION = "COMPETITOR_POSITION"
    NEW_ENTRANT_THREAT = "NEW_ENTRANT_THREAT"
    CHANNEL_SPECIALTY = "CHANNEL_SPECIALTY"
    SOURCE_DIFFERENCE = "SOURCE_DIFFERENCE"
    SALES_ACTIVITY_TREND = "SALES_ACTIVITY_TREND"
    SALES_IMPACT = "SALES_IMPACT"
    MULTI_SOURCE_SNAPSHOT = "MULTI_SOURCE_SNAPSHOT"
    EXTERNAL_LOOKUP = "EXTERNAL_LOOKUP"


class OperationMode(StrEnum):
    READ = "READ"
    SIDE_BY_SIDE = "SIDE_BY_SIDE"
    FORBIDDEN_SUM = "FORBIDDEN_SUM"
    PER_PATIENT = "PER_PATIENT"
    CAUSAL = "CAUSAL"


class SlotKind(StrEnum):
    FACT = "FACT"
    SCOPE = "SCOPE"
    POLICY = "POLICY"
    INTERPRETATION = "INTERPRETATION"


class MissingPolicy(StrEnum):
    FATAL = "FATAL"
    PARTIAL = "PARTIAL"
    NEVER_MISSING = "NEVER_MISSING"


@dataclass(frozen=True, slots=True)
class SlotSpec:
    id: str
    kind: SlotKind
    user_label: str
    missing_label: str
    missing_policy: MissingPolicy


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    intent: AnswerIntent
    blocking_required_slots: tuple[str, ...]
    partial_required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]
    forbidden_claim_types: tuple[str, ...]
    anchor_provenance: str | None = "USER_TEXT"
    operation_mode: OperationMode = OperationMode.READ

    @property
    def required_slots(self) -> tuple[str, ...]:
        return (*self.blocking_required_slots, *self.partial_required_slots)

    @property
    def entitled_slots(self) -> frozenset[str]:
        return frozenset((*self.required_slots, *self.optional_slots))

    def slot_spec(self, slot_id: str) -> SlotSpec:
        return SLOT_SPECS[slot_id]

    def with_anchor_provenance(self, value: str | None) -> QuestionSpec:
        return replace(self, anchor_provenance=value)


def _spec(
    intent: AnswerIntent,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
    *,
    partial: tuple[str, ...] = (),
) -> QuestionSpec:
    return QuestionSpec(intent, required, partial, optional, forbidden)


def _slot(
    slot_id: str,
    kind: SlotKind,
    user_label: str,
    missing_label: str,
    policy: MissingPolicy = MissingPolicy.FATAL,
) -> SlotSpec:
    return SlotSpec(slot_id, kind, user_label, missing_label, policy)


_SLOT_ROWS = (
    _slot("latest_market_size", SlotKind.FACT, "최신 시장 규모", "최신 시장 규모 데이터"),
    _slot("market_size_trend", SlotKind.FACT, "시장 규모 변화", "시장 규모 추이 데이터"),
    _slot("brand_sales_series", SlotKind.FACT, "브랜드 매출 추이", "브랜드 매출 추이 데이터"),
    _slot("brand_trend_conclusion", SlotKind.INTERPRETATION, "브랜드 추이 결론", "브랜드 추이 결론 근거"),
    _slot("recent_observed_trend", SlotKind.FACT, "최근 관측 추세", "최근 관측 추세 데이터"),
    _slot("forecast_basis", SlotKind.FACT, "조건부 전망", "조건부 전망 계산 근거"),
    _slot("risk_factors", SlotKind.POLICY, "전망 미반영 요인", "전망 위험요인 설명", MissingPolicy.NEVER_MISSING),
    _slot("forecast_availability", SlotKind.POLICY, "전망 제공 범위", "전망 제공 범위 설명", MissingPolicy.NEVER_MISSING),
    _slot("uncertainty", SlotKind.POLICY, "전망 불확실성", "전망 불확실성 설명", MissingPolicy.NEVER_MISSING),
    _slot("comparison_period", SlotKind.SCOPE, "비교 기간", "비교 기간 정보"),
    _slot("current_top_structure", SlotKind.FACT, "현재 경쟁 구도", "현재 경쟁 구도 데이터"),
    _slot("share_gainers", SlotKind.FACT, "점유율 상승 브랜드", "점유율 상승 데이터"),
    _slot("share_losers", SlotKind.FACT, "점유율 하락 브랜드", "점유율 하락 데이터"),
    _slot("competition_change_conclusion", SlotKind.INTERPRETATION, "경쟁 구도 변화", "경쟁 구도 변화 근거"),
    _slot("competitor_definition", SlotKind.SCOPE, "경쟁군 정의", "경쟁군 정의"),
    _slot("own_position", SlotKind.FACT, "자사 위치", "자사 위치 데이터"),
    _slot("competitor_comparison", SlotKind.INTERPRETATION, "경쟁사 비교", "경쟁사 비교 데이터"),
    _slot("new_observation_basis", SlotKind.SCOPE, "신규 관찰 기준", "신규 관찰 기준"),
    _slot("threat_evidence", SlotKind.FACT, "위협 근거", "위협 판단 데이터"),
    _slot("threat_conclusion", SlotKind.INTERPRETATION, "위협 판단", "위협 판단 근거"),
    _slot("channel_distribution", SlotKind.FACT, "채널별 분포", "채널별 분포 데이터", MissingPolicy.PARTIAL),
    _slot("specialty_distribution", SlotKind.FACT, "진료과별 분포", "진료과별 분포 데이터", MissingPolicy.PARTIAL),
    _slot("measurement_subject_difference", SlotKind.SCOPE, "측정 대상 차이", "측정 대상 정의"),
    _slot("distribution_stage_difference", SlotKind.SCOPE, "유통 단계 차이", "유통 단계 정의"),
    _slot("cadence_difference", SlotKind.SCOPE, "집계 주기 차이", "집계 주기 정의"),
    _slot("direct_comparison_limit", SlotKind.POLICY, "직접 비교 제한", "직접 비교 제한 설명", MissingPolicy.NEVER_MISSING),
    _slot("competitor_activity_change", SlotKind.FACT, "경쟁사 활동 변화", "경쟁사 활동 변화 데이터"),
    _slot("coverage_and_missingness", SlotKind.SCOPE, "조회 범위", "조회 범위 정보"),
    _slot("activity_series", SlotKind.FACT, "영업활동 추이", "영업활동 추이 데이터"),
    _slot("activity_coverage", SlotKind.SCOPE, "영업활동 조회 범위", "영업활동 조회 범위"),
    _slot("activity_change", SlotKind.FACT, "영업활동 변화", "영업활동 변화 데이터"),
    _slot("performance_change", SlotKind.FACT, "매출 변화", "매출 변화 데이터"),
    _slot("temporal_alignment", SlotKind.SCOPE, "비교 시점 정렬", "비교 시점 정보"),
    _slot("noncausal_limit", SlotKind.POLICY, "영향 판단 제한", "영향 판단 제한 설명", MissingPolicy.NEVER_MISSING),
    _slot("patient_count", SlotKind.FACT, "환자 수", "환자 수 데이터"),
    _slot("sales_value", SlotKind.FACT, "매출", "매출 데이터"),
    _slot("source_separation_limit", SlotKind.POLICY, "소스 분리 원칙", "소스 분리 원칙", MissingPolicy.NEVER_MISSING),
    _slot("capability_level", SlotKind.SCOPE, "조회 범위", "조회 가능 범위"),
    _slot("selection_basis", SlotKind.SCOPE, "선정 기준", "결과 선정 기준"),
    _slot("result_items", SlotKind.FACT, "최근 이슈", "최근 이슈 데이터"),
    _slot("internal_brand_metric", SlotKind.FACT, "내부 정형 지표", "내부 정형 지표"),
)
SLOT_SPECS: Final = MappingProxyType({item.id: item for item in _SLOT_ROWS})


QUESTION_CONTRACTS: Final = MappingProxyType({
    AnswerIntent.MARKET_SIZE_TREND: _spec(
        AnswerIntent.MARKET_SIZE_TREND,
        ("latest_market_size", "market_size_trend"),
        ("market_cagr", "channel_breakdown"),
    ),
    AnswerIntent.BRAND_TREND: _spec(
        AnswerIntent.BRAND_TREND,
        ("brand_sales_series", "brand_trend_conclusion"),
        ("market_comparison", "prescription_series"),
    ),
    AnswerIntent.MARKET_OUTLOOK: _spec(
        AnswerIntent.MARKET_OUTLOOK,
        ("recent_observed_trend", "forecast_basis", "risk_factors", "forecast_availability", "uncertainty"),
        ("scenario_range", "patient_trend", "impacting_issues"),
        ("competitor_sales_activity", "source_definition"),
    ),
    AnswerIntent.COMPETITION_CHANGE: _spec(
        AnswerIntent.COMPETITION_CHANGE,
        ("comparison_period", "current_top_structure", "share_gainers", "share_losers", "competition_change_conclusion"),
        ("own_position_change", "rank_changes", "absolute_sales_change", "hhi_change", "share_of_growth", "news"),
        ("current_ranking_only",),
    ),
    AnswerIntent.COMPETITOR_POSITION: _spec(
        AnswerIntent.COMPETITOR_POSITION,
        ("competitor_definition", "own_position", "competitor_comparison"),
        ("cohort_z_score", "channel_position"),
    ),
    AnswerIntent.NEW_ENTRANT_THREAT: _spec(
        AnswerIntent.NEW_ENTRANT_THREAT,
        ("new_observation_basis", "threat_evidence", "threat_conclusion"),
        ("launch_acceleration", "connected_issues"),
        ("own_share_decline_substitute", "arbitrary_brand_list"),
    ),
    AnswerIntent.CHANNEL_SPECIALTY: _spec(
        AnswerIntent.CHANNEL_SPECIALTY,
        (),
        ("channel_growth_difference", "leading_axis_conclusion"),
        partial=("channel_distribution", "specialty_distribution"),
    ),
    AnswerIntent.SOURCE_DIFFERENCE: _spec(
        AnswerIntent.SOURCE_DIFFERENCE,
        ("measurement_subject_difference", "distribution_stage_difference", "cadence_difference", "direct_comparison_limit"),
        ("source_values", "source_lag"),
        ("brand_growth_rate", "brand_ranking", "brand_share_trend"),
    ),
    AnswerIntent.SALES_ACTIVITY_TREND: _spec(
        AnswerIntent.SALES_ACTIVITY_TREND,
        ("competitor_activity_change", "comparison_period", "coverage_and_missingness"),
        ("seller_change_conclusion", "topic_by_seller"),
        ("own_prescription_sales_trend",),
    ),
    AnswerIntent.SALES_IMPACT: _spec(
        AnswerIntent.SALES_IMPACT,
        ("activity_change", "performance_change", "temporal_alignment", "noncausal_limit"),
        ("topic_alignment",),
        ("causal_assertion",),
    ),
    AnswerIntent.MULTI_SOURCE_SNAPSHOT: _spec(
        AnswerIntent.MULTI_SOURCE_SNAPSHOT,
        ("patient_count", "sales_value", "source_separation_limit"),
        ("period_alignment",),
        ("cross_source_sum", "per_patient_without_population_alignment", "causal_assertion"),
    ),
    AnswerIntent.EXTERNAL_LOOKUP: _spec(
        AnswerIntent.EXTERNAL_LOOKUP,
        ("capability_level", "selection_basis", "result_items"),
        ("total_count", "shown_count", "filters", "missingness", "internal_brand_metric"),
        ("sample_as_complete_analysis",),
    ),
})


D1_QUESTION_SPEC: Final = _spec(
    AnswerIntent.SALES_ACTIVITY_TREND,
    ("activity_series", "activity_change", "activity_coverage"),
    ("topic_by_seller",),
    ("competitor_activity_change", "own_prescription_sales_trend"),
)


def _operation_mode(question: str) -> OperationMode:
    if re.search(r"합산|합쳐\s*(?:줘|주세요)|더해\s*(?:줘|주세요)", question):
        return OperationMode.FORBIDDEN_SUM
    if re.search(r"환자당\s*(?:매출|처방)", question):
        return OperationMode.PER_PATIENT
    if re.search(r"영향\s*(?:줬|주었|미쳤|있)", question):
        return OperationMode.CAUSAL
    if re.search(r"한번에|나란히|같이\s*보", question):
        return OperationMode.SIDE_BY_SIDE
    return OperationMode.READ


def intent_for_question(question: str) -> AnswerIntent:
    folded = question.casefold()
    if "iqvia" in folded and "ubist" in folded and re.search(r"왜|다르", question):
        return AnswerIntent.SOURCE_DIFFERENCE
    if re.search(r"환자\s*수", question) and re.search(r"매출", question):
        return AnswerIntent.MULTI_SOURCE_SNAPSHOT
    if re.search(r"경쟁사", question) and re.search(r"영업\s*활동", question):
        return AnswerIntent.SALES_ACTIVITY_TREND
    if re.search(r"영업\s*활동", question) and re.search(r"영향", question):
        return AnswerIntent.SALES_IMPACT
    if re.search(r"신규\s*진입|위협\s*브랜드", question):
        return AnswerIntent.NEW_ENTRANT_THREAT
    if re.search(r"경쟁\s*구도", question) and re.search(r"변|최근", question):
        return AnswerIntent.COMPETITION_CHANGE
    if re.search(r"경쟁\s*상대|우리\s*위치", question):
        return AnswerIntent.COMPETITOR_POSITION
    if re.search(r"채널|진료과", question):
        return AnswerIntent.CHANNEL_SPECIALTY
    if re.search(r"앞으로|전망|예측|될\s*것", question):
        return AnswerIntent.MARKET_OUTLOOK
    if re.search(r"시장\s*규모", question):
        return AnswerIntent.MARKET_SIZE_TREND
    if re.search(r"영업\s*활동", question):
        return AnswerIntent.SALES_ACTIVITY_TREND
    if re.search(r"매출|처방", question):
        return AnswerIntent.BRAND_TREND
    return AnswerIntent.EXTERNAL_LOOKUP


def question_spec_for(question: str) -> QuestionSpec:
    intent = intent_for_question(question)
    base = (
        D1_QUESTION_SPEC
        if intent is AnswerIntent.SALES_ACTIVITY_TREND and "경쟁사" not in question
        else QUESTION_CONTRACTS[intent]
    )
    return replace(base, operation_mode=_operation_mode(question))
