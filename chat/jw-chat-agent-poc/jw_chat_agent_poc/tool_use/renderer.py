from __future__ import annotations

from jw_chat_agent_poc.tool_use.contracts import EvidenceFact


def render_evidence_answer(facts: tuple[EvidenceFact, ...]) -> str:
    if not facts:
        return "확인 가능한 근거가 없어 답변할 수 없습니다."
    lines = [render_evidence_claim(fact) for fact in facts]
    if any(fact.source_name == "FDA 이상반응 보고 정보" for fact in facts):
        lines.append("주의: FAERS 자발보고는 약물과 반응의 인과관계를 입증하지 않습니다.")
    if _has_clinical_trials_list_counts(facts):
        lines.append(
            "범위: 등록 목록만 표시합니다. "
            "의약품별 집계, 순위, 경쟁 분석/서사는 제공하지 않습니다."
        )
    return "\n".join(lines)


def render_evidence_claim(fact: EvidenceFact) -> str:
    is_numeric = fact.value is not None
    value = str(fact.value) if is_numeric else (fact.source_locator or "확인됨")
    unit = (fact.unit or "") if is_numeric else ""
    period = f" ({fact.period})" if fact.period else ""
    locator = f" · {fact.source_locator}" if is_numeric and fact.source_locator else ""
    return (
        f"- {fact.subject}{period}: {fact.metric} = {value}{unit} "
        f"[{fact.source_name}{locator}]"
    )


def _has_clinical_trials_list_counts(facts: tuple[EvidenceFact, ...]) -> bool:
    metrics = {fact.metric for fact in facts if fact.source_name == "ClinicalTrials.gov 임상시험 정보"}
    return {"현재 연결 조회 건수", "표시 건수"}.issubset(metrics)
