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


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    intent: AnswerIntent
    required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]
    forbidden_claim_types: tuple[str, ...]
    anchor_provenance: str | None = "USER_TEXT"
    operation_mode: OperationMode = OperationMode.READ

    @property
    def entitled_slots(self) -> frozenset[str]:
        return frozenset((*self.required_slots, *self.optional_slots))

    def with_anchor_provenance(self, value: str | None) -> QuestionSpec:
        return replace(self, anchor_provenance=value)


def _spec(
    intent: AnswerIntent,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
) -> QuestionSpec:
    return QuestionSpec(intent, required, optional, forbidden)


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
        ("channel_distribution", "specialty_distribution"),
        ("channel_growth_difference", "leading_axis_conclusion"),
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
        ("total_count", "shown_count", "filters", "missingness"),
        ("sample_as_complete_analysis",),
    ),
})


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
    base = QUESTION_CONTRACTS[intent_for_question(question)]
    return replace(base, operation_mode=_operation_mode(question))
