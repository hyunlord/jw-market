from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.orchestrator.provenance_model import sanitize_internal_provenance_labels


DUAL_WARNING = "전략뷰와 일반뷰는 시장 구성과 분모가 달라 수치를 직접 비교할 수 없습니다"
IQVIA_GENERAL_WARNING = (
    "이 값은 일반뷰(IQVIA NSA, ATC4 기준)입니다. "
    "전략뷰(UBIST) 값과 분모·기간이 달라 직접 비교할 수 없습니다."
)


def enforce_general_view_contract(answer: str, contract: dict[str, Any] | None) -> str:
    if not contract:
        return answer
    section = sanitize_internal_provenance_labels(str(contract.get("section_markdown") or "")).strip()
    if not section:
        return answer
    labels = _scope_labels(contract)
    strategic_answer = answer.rstrip()
    if contract.get("mode") == "dual" and not strategic_answer.startswith("## 전략뷰 (market_landscape)"):
        strategic_answer = f"## 전략뷰 (market_landscape)\n\n{strategic_answer}"
    parts = [strategic_answer]
    if not _section_present(answer, section):
        parts.append(section)
    combined = "\n\n".join(part for part in parts if part)
    for label in labels:
        if label not in combined:
            combined = "\n\n".join(part for part in (combined, label) if part)
    return append_general_view_warning(combined, contract)


def append_general_view_warning(answer: str, contract: dict[str, Any] | None) -> str:
    if not contract or contract.get("mode") not in {"dual", "general_only"}:
        return answer
    warning = IQVIA_GENERAL_WARNING if _uses_iqvia(contract) else DUAL_WARNING
    if warning in answer:
        return answer
    return "\n\n".join((answer.rstrip(), f"> {warning}"))


def _uses_iqvia(contract: dict[str, Any]) -> bool:
    sections = contract.get("atc4_sections")
    scopes = sections if isinstance(sections, list) and sections else [contract]
    return any(
        "IQVIA" in str(scope.get("source") or "").upper()
        for scope in scopes
        if isinstance(scope, dict)
    )


def _section_present(answer: str, section: str) -> bool:
    if section in answer:
        return True
    heading, separator, body = section.partition("\n")
    if heading.strip() and any(line.strip() == heading.strip() for line in answer.splitlines()):
        return True
    return bool(separator and body.strip() and body.strip() in answer)


def _scope_labels(contract: dict[str, Any]) -> tuple[str, ...]:
    sections = contract.get("atc4_sections")
    scopes = sections if isinstance(sections, list) and sections else [contract]
    return tuple(
        "기준: 일반뷰 "
        f"(ATC4 {str(scope.get('atc4_code') or '')}) | "
        f"소스: {str(scope.get('source') or '')} | "
        f"지표: {str(scope.get('measure') or '')} | "
        f"기준: {str(scope.get('period') or '')}"
        for scope in scopes
        if isinstance(scope, dict)
    )
