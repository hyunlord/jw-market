from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from jw_chat_agent_poc.service.v4.contracts import (
    AnswerContract,
    AnswerShape,
    RequestedAnswerShape,
    RequiredAnswerItem,
    SourceName,
)

_PERIOD_RE = re.compile(r"(?<!\d)(20\d{2})[년./-]\s*(0?[1-9]|1[0-2])(?:월)?")
_TOP_K_RE = re.compile(r"(?:상위\s*)?(\d{1,3})\s*(?:개|건|위)")
_RECENT_MONTH_RE = re.compile(r"최근\s*(\d{1,3})\s*개월")


@dataclass(frozen=True)
class ContractSlotCoverage:
    entity_coverage: float
    metric_coverage: float
    period_coverage: float
    dimension_coverage: float
    required_source_coverage: float
    missing_entities: tuple[str, ...]
    missing_metrics: tuple[str, ...]
    missing_periods: tuple[str, ...]
    missing_dimensions: tuple[str, ...]
    missing_sources: tuple[SourceName, ...]
    complete: bool


class ClaimLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


@dataclass(frozen=True)
class ClaimFact:
    fact_id: str
    source: str
    entity: str
    metric: str
    period: str | None = None
    unit: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ClaimCandidate:
    text: str
    claim_level: ClaimLevel
    supporting_fact_ids: tuple[str, ...]
    inference_kind: str | None = None
    subject: str | None = None
    choice: str | None = None


@dataclass(frozen=True)
class ClaimEligibility:
    eligible: bool
    reason: str
    supporting_fact_ids: tuple[str, ...]


_FORBIDDEN_INFERENCES = frozenset(
    {
        "clinical_count_to_efficacy",
        "sales_to_prescription_preference",
        "change_date_to_management_capability",
        "patent_expiry_to_generic_certainty",
        "absence_to_development_stagnation",
        "applied_rows_to_transactions",
    }
)


def derive_answer_contract(
    question: str,
    requested_shape: RequestedAnswerShape,
    *,
    answer_sources: Sequence[SourceName],
) -> AnswerContract:
    normalized = " ".join(question.split())
    lowered = normalized.casefold()
    answer_shape = _answer_shape(lowered, answer_sources=answer_sources)
    metrics = _required_metrics(normalized, requested_shape, answer_shape)
    dimensions = _required_dimensions(lowered, requested_shape, answer_shape)
    periods = _required_periods(normalized, requested_shape)
    required_sources = _required_sources(
        answer_shape,
        answer_sources,
        metrics=metrics,
        lowered=lowered,
    )
    top_k = _top_k(normalized) if answer_shape is AnswerShape.RANKING else None
    required_period_count = (
        requested_shape.period_count
        or _required_period_count(normalized, answer_shape)
    )

    forbidden: list[str] = []
    denominator_policy = "not_applicable"
    if answer_shape is AnswerShape.MULTI_FIELD_LOOKUP:
        forbidden.append("representative_row")
    elif answer_shape is AnswerShape.TIME_SERIES:
        forbidden.append("latest_scalar_for_time_series")
    elif answer_shape is AnswerShape.RANKING:
        forbidden.append("top_k_to_single_representative")
        denominator_policy = "same_scope_total"
    elif answer_shape in {AnswerShape.COMPARISON, AnswerShape.GROUP_DISTRIBUTION}:
        denominator_policy = "same_grain_only"
    elif answer_shape is AnswerShape.POLICY_TEXT:
        forbidden.extend(("patent_for_policy", "approval_for_policy"))

    return AnswerContract(
        question_core=normalized,
        resolved_entities=tuple(dict.fromkeys(requested_shape.entities)),
        required_entities=tuple(dict.fromkeys(requested_shape.entities)),
        required_metrics=metrics,
        required_periods=periods,
        required_dimensions=dimensions,
        answer_shape=answer_shape,
        required_sources=required_sources,
        denominator_policy=denominator_policy,
        forbidden_substitutions=tuple(forbidden),
        top_k=top_k,
        required_period_count=required_period_count,
    )


def merge_interpretation_contract(
    derived: AnswerContract,
    interpreted: AnswerContract | None,
    question: str,
) -> AnswerContract:
    """Keep planner-owned answer obligations while refreshing semantic slots."""

    normalized_question = " ".join(question.split())
    if interpreted is None:
        interpreted = AnswerContract(question_core=normalized_question)
    question_core = " ".join(interpreted.question_core.split()) or normalized_question
    required_items: list[RequiredAnswerItem] = []
    seen_ids: set[str] = set()
    for item in interpreted.required_items:
        if item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        required_items.append(item)
    if not required_items:
        required_items.extend(_deterministic_required_items(normalized_question))
    resolved_entities = tuple(
        dict.fromkeys(
            entity.strip()
            for entity in (*interpreted.resolved_entities, *derived.required_entities)
            if entity.strip()
        )
    )
    return derived.model_copy(
        update={
            "question_core": question_core,
            "required_items": tuple(required_items),
            "resolved_entities": resolved_entities,
            "required_items_degraded": not required_items,
        }
    )


def _deterministic_required_items(question: str) -> tuple[RequiredAnswerItem, ...]:
    """Recover explicit multi-part obligations when planner output degrades."""

    normalized = " ".join(question.split())
    lowered = normalized.casefold()
    items: list[RequiredAnswerItem] = []
    if any(marker in lowered for marker in ("보다", "대비", "비교")) and any(
        marker in lowered for marker in ("성장", "크고", "빠르")
    ):
        items.append(
            RequiredAnswerItem(
                id="growth_comparison",
                ask="비교 대상 대비 성장 속도",
                kind="data",
            )
        )
    if any(marker in lowered for marker in ("왜", "이유", "원인")):
        items.append(
            RequiredAnswerItem(
                id="growth_reason" if items else "reason",
                ask="관찰된 차이의 근거 기반 해석",
                kind="reading",
            )
        )
    return tuple(items)


def contract_slot_coverage(
    contract: AnswerContract,
    *,
    source_states: Mapping[str, str],
    fulfilled_entities: Sequence[str],
    fulfilled_metrics: Sequence[str],
    fulfilled_periods: Sequence[str],
    fulfilled_dimensions: Sequence[str],
) -> ContractSlotCoverage:
    missing_entities = _missing(contract.required_entities, fulfilled_entities)
    missing_metrics = _missing(contract.required_metrics, fulfilled_metrics)
    missing_periods = _missing(contract.required_periods, fulfilled_periods)
    missing_dimensions = _missing(contract.required_dimensions, fulfilled_dimensions)
    success_states = {"EXECUTED_SUCCESS", "ok"}
    missing_sources = tuple(
        source
        for source in contract.required_sources
        if source_states.get(source) not in success_states
    )
    coverage = ContractSlotCoverage(
        entity_coverage=_coverage(contract.required_entities, missing_entities),
        metric_coverage=_coverage(contract.required_metrics, missing_metrics),
        period_coverage=_coverage(contract.required_periods, missing_periods),
        dimension_coverage=_coverage(contract.required_dimensions, missing_dimensions),
        required_source_coverage=_coverage(contract.required_sources, missing_sources),
        missing_entities=missing_entities,
        missing_metrics=missing_metrics,
        missing_periods=missing_periods,
        missing_dimensions=missing_dimensions,
        missing_sources=missing_sources,
        complete=not any(
            (
                missing_entities,
                missing_metrics,
                missing_periods,
                missing_dimensions,
                missing_sources,
            )
        ),
    )
    return coverage


def evaluate_claim(
    claim: ClaimCandidate,
    facts: Sequence[ClaimFact],
) -> ClaimEligibility:
    facts_by_id = {fact.fact_id: fact for fact in facts}
    supporting = tuple(
        fact_id for fact_id in claim.supporting_fact_ids if fact_id in facts_by_id
    )
    if claim.inference_kind in _FORBIDDEN_INFERENCES:
        return ClaimEligibility(False, "FORBIDDEN_INFERENCE", supporting)
    if claim.claim_level is ClaimLevel.L3 and (
        not claim.subject or not claim.choice or not supporting
    ):
        return ClaimEligibility(
            False,
            "L3_MISSING_SUBJECT_CHOICE_EVIDENCE",
            supporting,
        )
    if not supporting:
        return ClaimEligibility(False, "MISSING_SUPPORTING_FACT", ())
    if claim.claim_level is ClaimLevel.L2 and not _has_comparison_basis(
        tuple(facts_by_id[fact_id] for fact_id in supporting)
    ):
        return ClaimEligibility(False, "L2_MISSING_COMPARISON_BASIS", supporting)
    return ClaimEligibility(True, "ELIGIBLE", supporting)


def _answer_shape(
    lowered: str,
    *,
    answer_sources: Sequence[SourceName] = (),
) -> AnswerShape:
    if any(token in lowered for token in ("급여기준", "급여 기준", "고시 원문", "정책 원문")):
        return AnswerShape.POLICY_TEXT
    document_available = "document" in answer_sources
    phase_list_request = bool(
        re.search(r"(?:[1-4]\s*상|phase\s*(?:[1-4]|i{1,3}|iv))", lowered)
        and any(token in lowered for token in ("목록", "정리", "모두", "전부"))
    )
    if document_available and phase_list_request:
        return AnswerShape.DOCUMENT_LIST_EXTRACT
    if any(token in lowered for token in ("문서", "파일", "업로드")):
        if any(token in lowered for token in ("목록", "모두 정리", "전부 정리", "시험을 정리")):
            return AnswerShape.DOCUMENT_LIST_EXTRACT
        if any(token in lowered for token in ("요약", "정리해줘", "내용 알려줘")):
            return AnswerShape.DOCUMENT_SUMMARY
    if any(token in lowered for token in ("상위", "순위", "가장", "최대", "비중이 높은")):
        return AnswerShape.RANKING
    if any(token in lowered for token in ("채널별", "거래처별", "지역별", "분포")):
        return AnswerShape.GROUP_DISTRIBUTION
    if any(token in lowered for token in ("추이", "시계열", "월별", "연도별", "분기별")):
        return AnswerShape.TIME_SERIES
    if any(token in lowered for token in ("비교", "대비", "성장률", "yoy")):
        return AnswerShape.COMPARISON
    multi_fields = sum(
        token in lowered
        for token in (
            "허가일",
            "재심사기간",
            "재심사 기간",
            "재심사 만료일",
            "변경일",
        )
    )
    if "품목별" in lowered or multi_fields >= 2:
        return AnswerShape.MULTI_FIELD_LOOKUP
    return AnswerShape.SCALAR_LOOKUP


def _required_metrics(
    question: str,
    requested_shape: RequestedAnswerShape,
    answer_shape: AnswerShape,
) -> tuple[str, ...]:
    metrics: list[str] = []
    for canonical, markers in (
        ("허가일", ("허가일",)),
        (
            "재심사기간",
            ("재심사기간", "재심사 기간", "재심사 만료일", "재심사 만료"),
        ),
        ("변경일", ("변경일", "변경 이력")),
        ("급여기준", ("급여기준", "급여 기준", "고시")),
        ("매출비중", ("매출비중", "매출 비중")),
        ("매출", ("매출", "sellout", "sell-out", "sell in", "sell-in")),
        ("환자수", ("환자수", "환자 수")),
        ("특허", ("특허",)),
        ("임상시험", ("임상", "시험")),
    ):
        if any(marker in question.casefold() for marker in markers):
            metrics.append(canonical)
    if answer_shape is AnswerShape.DOCUMENT_SUMMARY and not metrics:
        metrics.append("문서 요약")
    metrics.extend(requested_shape.measure_or_attribute)
    return tuple(dict.fromkeys(metrics))


def _required_dimensions(
    lowered: str,
    requested_shape: RequestedAnswerShape,
    answer_shape: AnswerShape,
) -> tuple[str, ...]:
    dimensions: list[str] = []
    if answer_shape is AnswerShape.MULTI_FIELD_LOOKUP and "품목" in lowered:
        dimensions.append("품목")
    if answer_shape is AnswerShape.TIME_SERIES:
        dimensions.append(requested_shape.granularity or "period")
    if answer_shape is AnswerShape.COMPARISON:
        dimensions.append("comparison_period")
    if answer_shape is AnswerShape.RANKING:
        dimensions.append("rank")
    for marker, dimension in (
        ("채널별", "channel"),
        ("거래처별", "account"),
        ("지역별", "region"),
    ):
        if marker in lowered:
            dimensions.append(dimension)
    return tuple(dict.fromkeys(dimensions))


def _required_periods(
    question: str,
    requested_shape: RequestedAnswerShape,
) -> tuple[str, ...]:
    periods = [f"{year}-{int(month):02d}" for year, month in _PERIOD_RE.findall(question)]
    if requested_shape.period_from:
        periods.append(requested_shape.period_from)
    if requested_shape.period_to:
        periods.append(requested_shape.period_to)
    return tuple(dict.fromkeys(periods))


def _required_sources(
    answer_shape: AnswerShape,
    answer_sources: Sequence[SourceName],
    *,
    metrics: Sequence[str],
    lowered: str,
) -> tuple[SourceName, ...]:
    if answer_shape is AnswerShape.POLICY_TEXT:
        return ("hira",)
    if answer_shape in {
        AnswerShape.DOCUMENT_LIST_EXTRACT,
        AnswerShape.DOCUMENT_SUMMARY,
    }:
        return ("document",)
    available = set(answer_sources)
    required: list[SourceName] = []
    for metric, source in (
        ("급여기준", "hira"),
        ("환자수", "hira"),
        ("특허", "patent"),
        ("임상시험", "clinicaltrials"),
        ("허가일", "nedrug"),
        ("재심사기간", "nedrug"),
        ("변경일", "nedrug"),
    ):
        if metric in metrics and source in available and source not in required:
            required.append(source)
    if any(metric in metrics for metric in ("매출", "매출비중")):
        file_explicit = any(
            marker in lowered for marker in ("파일", "문서", "업로드", "엑셀")
        )
        if file_explicit and "document" in available:
            required.append("document")
        if "mart" in available and (not file_explicit or "리바로" in lowered):
            required.append("mart")
    if required:
        return tuple(dict.fromkeys(required))
    non_enrichment = tuple(
        source for source in answer_sources if source not in {"web", "openfda"}
    )
    return non_enrichment[:1] or tuple(answer_sources[:1])


def _top_k(question: str) -> int:
    match = _TOP_K_RE.search(question)
    return min(100, int(match.group(1))) if match else 5


def _required_period_count(question: str, answer_shape: AnswerShape) -> int | None:
    if answer_shape is not AnswerShape.TIME_SERIES:
        return None
    match = _RECENT_MONTH_RE.search(question)
    return min(120, int(match.group(1))) if match else None


def _missing(required: Sequence[str], fulfilled: Sequence[str]) -> tuple[str, ...]:
    fulfilled_keys = {str(value).casefold() for value in fulfilled}
    return tuple(value for value in required if str(value).casefold() not in fulfilled_keys)


def _coverage(required: Sequence[object], missing: Sequence[object]) -> float:
    return 1.0 if not required else round((len(required) - len(missing)) / len(required), 4)


def _has_comparison_basis(facts: Sequence[ClaimFact]) -> bool:
    if len(facts) < 2:
        return False
    for index, first in enumerate(facts):
        for second in facts[index + 1 :]:
            if (
                first.metric == second.metric
                and first.unit == second.unit
                and first.period == second.period
                and tuple(name for name, _value in first.dimensions)
                == tuple(name for name, _value in second.dimensions)
            ):
                return True
    return False


__all__ = [
    "AnswerShape",
    "ClaimCandidate",
    "ClaimEligibility",
    "ClaimFact",
    "ClaimLevel",
    "ContractSlotCoverage",
    "contract_slot_coverage",
    "derive_answer_contract",
    "evaluate_claim",
    "merge_interpretation_contract",
]
