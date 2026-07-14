from __future__ import annotations

from jw_chat_agent_poc.tool_use.contracts import EvidenceFact


def render_evidence_answer(facts: tuple[EvidenceFact, ...]) -> str:
    if not facts:
        return "확인 가능한 근거가 없어 답변할 수 없습니다."
    lines: list[str] = []
    for fact in facts:
        is_numeric = fact.value is not None
        value = str(fact.value) if is_numeric else (fact.source_locator or "확인됨")
        unit = (fact.unit or "") if is_numeric else ""
        period = f" ({fact.period})" if fact.period else ""
        locator = f" · {fact.source_locator}" if is_numeric and fact.source_locator else ""
        lines.append(
            f"- {fact.subject}{period}: {fact.metric} = {value}{unit} "
            f"[{fact.source_name}{locator}]"
        )
    return "\n".join(lines)
