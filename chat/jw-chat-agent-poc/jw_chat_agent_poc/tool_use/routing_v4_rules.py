from __future__ import annotations

from dataclasses import dataclass
import re

from jw_chat_agent_poc.orchestrator.hira_disease import (
    HIRA_TREND_YEARS,
    explicit_hira_disease_code,
    is_hira_disease_question,
)
from jw_chat_agent_poc.tool_use.routing_v4_types import DomainDecisionSource, ProposedCall


@dataclass(frozen=True, slots=True)
class QuestionClassification:
    source_domain: str
    domain_decision_source: DomainDecisionSource
    requested_capability: str
    deterministic_rule_id: str | None = None
    direct_calls: tuple[ProposedCall, ...] = ()
    eligible_override: tuple[str, ...] = ()
    unresolved_arguments: bool = False


PREFIX_RE = re.compile(r"^\s*(?P<prefix>NeDrug|HIRA|ClinicalTrials)\s*:\s*", re.IGNORECASE)
NCT_ID_RE = re.compile(r"(?<![A-Za-z0-9])NCT\d{8}(?![A-Za-z0-9])", re.IGNORECASE)


def classify_question(question: str) -> QuestionClassification:
    prefix_match = PREFIX_RE.match(question)
    body = question[prefix_match.end() :] if prefix_match else question
    prefix = prefix_match.group("prefix").casefold() if prefix_match else None
    lowered = body.casefold()
    code = explicit_disease_code(body)
    nct_id = NCT_ID_RE.search(body)

    if prefix == "hira":
        if asks_label_fields(lowered):
            return QuestionClassification(
                source_domain="hira",
                domain_decision_source=DomainDecisionSource.PREFIX_RULE,
                requested_capability="HIRA_LABEL_EFFICACY",
                deterministic_rule_id="SOURCE_PREFIX_HIRA",
            )
        return _hira_classification(body, code, DomainDecisionSource.PREFIX_RULE, "SOURCE_PREFIX_HIRA")
    if prefix == "nedrug":
        if asks_composition_fields(lowered):
            capability = "MFDS_COMPOSITION"
        elif asks_label_fields(lowered):
            capability = "MFDS_LABEL_EFFICACY"
        else:
            capability = "MFDS_BASIC_PRODUCT_INFO"
        return QuestionClassification(
            source_domain="regulatory",
            domain_decision_source=DomainDecisionSource.PREFIX_RULE,
            requested_capability=capability,
            deterministic_rule_id="SOURCE_PREFIX_NEDRUG",
            unresolved_arguments=capability == "MFDS_LABEL_EFFICACY" and _uses_product_family_reference(lowered),
        )
    if prefix == "clinicaltrials":
        return _clinical_classification(
            nct_id,
            DomainDecisionSource.PREFIX_RULE,
            "SOURCE_PREFIX_CLINICALTRIALS",
        )
    if code is not None:
        return _hira_classification(body, code, DomainDecisionSource.INTENT_OWNER, "DISEASE_CODE")
    if nct_id is not None:
        return _clinical_classification(nct_id, DomainDecisionSource.INTENT_OWNER, "NCT_ID")
    if any(token in lowered for token in ("급여", "reimbursement")):
        return QuestionClassification(
            source_domain="regulatory",
            domain_decision_source=DomainDecisionSource.INTENT_OWNER,
            requested_capability="REIMBURSEMENT_CRITERIA",
        )
    if asks_basic_permission_fields(lowered):
        return QuestionClassification(
            source_domain="regulatory",
            domain_decision_source=DomainDecisionSource.INTENT_OWNER,
            requested_capability="MFDS_BASIC_PRODUCT_INFO",
        )
    if any(token in lowered for token in ("임상시험", "clinical trial", "clinicaltrial")):
        return QuestionClassification(
            source_domain="clinical_trials",
            domain_decision_source=DomainDecisionSource.LLM,
            requested_capability="CLINICAL_TRIAL_SEARCH",
        )
    if is_hira_disease_question(question):
        return QuestionClassification(
            source_domain="hira",
            domain_decision_source=DomainDecisionSource.INTENT_OWNER,
            requested_capability="HIRA_DISEASE_PATIENT_STATS",
            unresolved_arguments=True,
        )
    return QuestionClassification(
        source_domain="unresolved",
        domain_decision_source=DomainDecisionSource.UNRESOLVED,
        requested_capability="UNCLASSIFIED_EXTERNAL_REQUEST",
    )


def explicit_disease_code(text: str) -> str | None:
    return explicit_hira_disease_code(text)


def asks_label_fields(lowered: str) -> bool:
    return any(
        token in lowered
        for token in ("효능", "효과", "용법", "용량", "주의사항", "precaution", "dosage", "efficacy")
    )


def asks_composition_fields(lowered: str) -> bool:
    return any(token in lowered for token in ("성분 조성", "성분·함량", "성분 함량"))


def asks_basic_permission_fields(lowered: str) -> bool:
    asks_permission = any(token in lowered for token in ("허가", "permission", "approval"))
    asks_basic_field = any(token in lowered for token in ("품목명", "업체명", "제조사", "manufacturer"))
    return asks_permission and asks_basic_field


def _uses_product_family_reference(lowered: str) -> bool:
    return any(token in lowered for token in ("제품의", "제품군", "제품별"))


def _hira_classification(
    body: str,
    code: str | None,
    decision_source: DomainDecisionSource,
    rule_id: str,
) -> QuestionClassification:
    if code is None:
        return QuestionClassification(
            source_domain="hira",
            domain_decision_source=decision_source,
            requested_capability="HIRA_DISEASE_PATIENT_STATS",
            deterministic_rule_id=rule_id,
            unresolved_arguments=True,
        )
    years = HIRA_TREND_YEARS if "추이" in body else ("2024",)
    calls = tuple(
        ProposedCall(
            tool_name="hira_disease_hospitalization_outpatient_stats",
            normalized_args={"sick_cd": code, "year": year},
        )
        for year in years
    )
    return QuestionClassification(
        source_domain="hira",
        domain_decision_source=decision_source,
        requested_capability="HIRA_DISEASE_PATIENT_STATS",
        deterministic_rule_id=rule_id,
        direct_calls=calls,
        eligible_override=("hira_disease_hospitalization_outpatient_stats",),
    )


def _clinical_classification(
    nct_match: re.Match[str] | None,
    decision_source: DomainDecisionSource,
    rule_id: str,
) -> QuestionClassification:
    capability = "CLINICAL_TRIAL_NCT_DETAIL_FIELDS" if nct_match is not None else "CLINICAL_TRIAL_SEARCH"
    calls = (
        (
            ProposedCall(
                tool_name="clinicaltrials_study_details",
                normalized_args={"nct_id": nct_match.group(0).upper()},
            ),
        )
        if nct_match is not None
        else ()
    )
    return QuestionClassification(
        source_domain="clinical_trials",
        domain_decision_source=decision_source,
        requested_capability=capability,
        deterministic_rule_id=rule_id,
        direct_calls=calls,
        eligible_override=("clinicaltrials_study_details",) if calls else (),
    )
