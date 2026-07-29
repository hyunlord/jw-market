from __future__ import annotations

import re
from typing import Any

from jw_chat_agent_poc.orchestrator.provenance_model import sanitize_internal_provenance_labels


DUAL_WARNING = "전략뷰와 일반뷰는 시장 구성과 분모가 달라 수치를 직접 비교할 수 없습니다"
STRATEGIC_HEADING = "## 전략뷰 (market_landscape)"
# Recognises the canonical strategic heading and its spacing variants only.
# Deliberately narrow: the line must be a level-2 heading whose whole text is
# 전략뷰, optionally qualified by (market_landscape). A heading that merely
# starts with 전략 ("## 전략적 판단") or carries extra words
# ("## 전략뷰 상세 분석") is not a strategic heading and is left alone.
_STRATEGIC_HEADING_LINE_RE = re.compile(
    r"^##\s*전략뷰(?:\s*\(\s*market_landscape\s*\))?\s*$"
)


def enforce_general_view_contract(answer: str, contract: dict[str, Any] | None) -> str:
    if not contract:
        return answer
    section = sanitize_internal_provenance_labels(str(contract.get("section_markdown") or "")).strip()
    if not section:
        return answer
    labels = _scope_labels(contract)
    strategic_answer = answer.rstrip()
    if contract.get("mode") == "dual":
        strategic_answer = _normalize_strategic_heading(strategic_answer)
    parts = [strategic_answer]
    if not _section_present(answer, section):
        parts.append(section)
    combined = "\n\n".join(part for part in parts if part)
    for label in labels:
        if label not in combined:
            combined = "\n\n".join(part for part in (combined, label) if part)
    if contract.get("mode") == "dual" and DUAL_WARNING not in combined:
        combined = "\n\n".join((combined, f"> {DUAL_WARNING}"))
    return combined


def strategic_heading_recognized(answer: str) -> bool:
    """True when the answer already opens with a recognisable strategic heading."""

    head, _separator, _rest = answer.partition("\n")
    return bool(_STRATEGIC_HEADING_LINE_RE.match(head.strip()))


def _normalize_strategic_heading(answer: str) -> str:
    """Keep exactly one canonical strategic heading at the top of the answer.

    An answer that already opens with a recognised heading variant has that line
    replaced by the canonical form, so no second heading is prepended. Anything
    else keeps its existing text and receives the canonical heading in front,
    exactly as before.
    """

    head, separator, rest = answer.partition("\n")
    if _STRATEGIC_HEADING_LINE_RE.match(head.strip()):
        return f"{STRATEGIC_HEADING}{separator}{rest}"
    return f"{STRATEGIC_HEADING}\n\n{answer}"


def _section_present(answer: str, section: str) -> bool:
    if section in answer:
        return True
    _, separator, body = section.partition("\n")
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
