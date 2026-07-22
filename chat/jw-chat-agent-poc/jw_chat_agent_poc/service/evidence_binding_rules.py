from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, number_tokens
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.tool_use.routing_v4_rules import explicit_disease_code


IDENTIFIER_KEYS: Final[tuple[str, ...]] = (
    "sick_cd",
    "nct_id",
    "brand_key",
    "brand",
    "market_id",
    "atc4_code",
)

_METRIC_TERMS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("초과성장", ("초과성장", "excess growth")),
    ("매출 변화율", ("매출 변화율", "성장률")),
    ("점유율 변화", ("점유율 변화", "점유율 증감")),
    ("환자수", ("환자수", "환자 수")),
    ("시장점유율", ("시장점유율", "점유율", "ms")),
    ("시장규모", ("시장규모", "시장 규모")),
    ("매출", ("매출", "sales")),
    ("HHI", ("hhi", "집중도")),
    ("CR5", ("cr5", "집중도")),
    ("CAGR", ("cagr", "연평균")),
    ("순위", ("순위", "rank")),
)
_ORDERED_LIST_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^\s*\d+[.)]\s+")
_SAME_ENTITY_OPERAND_METRICS: Final[frozenset[str]] = frozenset(
    {"점유율 변화", "매출 변화", "매출 변화율", "CAGR"}
)
_EXPECTED_OPERAND_METRICS: Final[dict[str, frozenset[str]]] = {
    "점유율 변화": frozenset({"시장점유율"}),
    "매출 변화": frozenset({"매출"}),
    "매출 변화율": frozenset({"매출"}),
    "CAGR": frozenset({"매출"}),
    "초과성장": frozenset({"CAGR", "매출 변화율"}),
}


def normalized_entity(value: str) -> str:
    stripped = str(value).strip().upper()
    code = explicit_disease_code(stripped)
    return code or stripped.casefold()


def expected_entity_set(question: str, values: Sequence[str]) -> set[str]:
    expected = {normalized_entity(value) for value in values if str(value).strip()}
    explicit = explicit_disease_code(question)
    if explicit:
        expected.add(normalized_entity(explicit))
    return expected


def question_metrics(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    metrics = [
        metric
        for metric, terms in _METRIC_TERMS
        if any(term.casefold() in lowered for term in terms)
    ]
    return tuple(dict.fromkeys(metrics))


def claim_metrics_for_token(answer: str, token: str) -> tuple[str, ...]:
    segments = re.split(r"(?<=[.!?。])\s+|\n+", answer)
    for segment in segments:
        if token in number_tokens(segment):
            return question_metrics(segment)
    return ()


def claim_number_tokens(text: str) -> tuple[str, ...]:
    return number_tokens(_ORDERED_LIST_MARKER_RE.sub("", text))


def explicit_periods(text: str) -> tuple[str, ...]:
    periods: list[str] = []
    periods.extend(
        re.findall(
            r"(?<!\d)20\d{2}-(?:0[1-9]|1[0-2]|Q[1-4])(?!\d)",
            text,
            re.IGNORECASE,
        )
    )
    periods.extend(
        re.findall(
            r"(?<!\d)20\d{2}(?!\d|-(?:0[1-9]|1[0-2]|Q[1-4]))",
            text,
            re.IGNORECASE,
        )
    )
    return tuple(dict.fromkeys(period.upper() for period in periods))


def entity_matches(fact: EvidenceFact, expected: set[str]) -> bool:
    if not expected:
        return True
    if not fact.entity:
        return False
    return normalized_entity(fact.entity) in expected


def metric_matches(fact: EvidenceFact, expected: tuple[str, ...]) -> bool:
    if not expected:
        return True
    return fact.metric in expected or fact.metric in {"기간", "질병코드"}


def period_matches(fact: EvidenceFact, requested: tuple[str, ...]) -> bool:
    if not requested or fact.metric in {"기간", "질병코드"}:
        return True
    if not present(fact.period):
        return False
    fact_periods = set(explicit_periods(fact.period))
    return bool(fact_periods.intersection(requested))


def unit_matches(fact: EvidenceFact, token: str) -> bool:
    expected = token_unit(token)
    if not expected or fact.metric in {"기간", "질병코드"}:
        return True
    if not present(fact.unit):
        return False
    if expected == "%":
        return fact.unit in {"%", "%p"}
    return fact.unit == expected


def mismatch_reason(
    candidates: Sequence[EvidenceFact],
    expected_entities: set[str],
    expected_metrics: tuple[str, ...],
    *,
    requested_periods: tuple[str, ...],
    token: str,
) -> str:
    if expected_entities and not any(
        entity_matches(fact, expected_entities) for fact in candidates
    ):
        return "ENTITY_MISMATCH"
    if expected_metrics and not any(
        metric_matches(fact, expected_metrics) for fact in candidates
    ):
        return "METRIC_MISMATCH"
    if requested_periods and not any(
        period_matches(fact, requested_periods) for fact in candidates
    ):
        return "PERIOD_MISMATCH"
    if not any(unit_matches(fact, token) for fact in candidates):
        return "UNIT_MISMATCH"
    return "BINDING_MISMATCH"


def grade(fact: EvidenceFact) -> SourceGrade:
    try:
        return SourceGrade(fact.source_grade)
    except ValueError:
        return SourceGrade.UNVERIFIED


def operand_binding_outcome(
    fact: EvidenceFact,
    facts_by_id: Mapping[str, EvidenceFact],
) -> tuple[str, str]:
    if not fact.operand_fact_ids:
        return "pass", ""
    operands = tuple(facts_by_id.get(fact_id) for fact_id in fact.operand_fact_ids)
    if any(operand is None for operand in operands):
        return "fail", "MISSING_OPERAND"
    bound_operands = tuple(operand for operand in operands if operand is not None)

    expected_metrics = _EXPECTED_OPERAND_METRICS.get(fact.metric)
    if expected_metrics:
        if any(not present(operand.metric) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(operand.metric not in expected_metrics for operand in bound_operands):
            return "fail", "OPERAND_METRIC_MISMATCH"

    if fact.metric in _SAME_ENTITY_OPERAND_METRICS and present(fact.entity):
        if any(not present(operand.entity) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        expected_entity = normalized_entity(fact.entity)
        if any(normalized_entity(operand.entity) != expected_entity for operand in bound_operands):
            return "fail", "OPERAND_ENTITY_MISMATCH"

    derived_periods = set(explicit_periods(fact.period))
    if derived_periods:
        operand_period_sets = tuple(set(explicit_periods(operand.period)) for operand in bound_operands)
        if any(not periods for periods in operand_period_sets):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(not periods.issubset(derived_periods) for periods in operand_period_sets):
            return "fail", "OPERAND_PERIOD_MISMATCH"

    if present(fact.view):
        if any(not present(operand.view) for operand in bound_operands):
            return "partial", "INCOMPLETE_OPERAND_BINDING"
        if any(operand.view != fact.view for operand in bound_operands):
            return "fail", "OPERAND_VIEW_MISMATCH"

    if any(not present(operand.source_grade) for operand in bound_operands):
        return "partial", "INCOMPLETE_OPERAND_BINDING"
    if any(grade(operand) is not grade(fact) for operand in bound_operands):
        return "fail", "OPERAND_SOURCE_GRADE_MISMATCH"
    return "pass", ""


def has_binding_metadata(fact: EvidenceFact) -> bool:
    return present(fact.entity) or present(fact.metric) or present(fact.source_grade)


def present(value: str) -> bool:
    return bool(value and value != "-")


def requested_period_unavailable(answer: str, requested: tuple[str, ...]) -> bool:
    if not any(period in answer.upper() for period in requested):
        return False
    return any(
        marker in answer
        for marker in (
            "조회 실패",
            "표시하지 않습니다",
            "확인할 수 없습니다",
            "데이터가 없습니다",
            "미보유",
        )
    )


def without_bound_identifiers(answer: str, expected: set[str]) -> str:
    stripped = answer
    for entity in expected:
        compact = entity.replace(".", "")
        for variant in (entity, compact):
            stripped = re.sub(re.escape(variant), "ENTITY", stripped, flags=re.IGNORECASE)
    return stripped


def token_unit(token: str) -> str:
    normalized = token.replace(" ", "")
    for suffix in ("억원", "억 원", "%p", "%", "명", "건", "개", "위", "원"):
        if normalized.endswith(suffix.replace(" ", "")):
            return suffix.replace(" ", "")
    return ""
