from __future__ import annotations

from typing import Any


DUAL_WARNING = "전략뷰와 일반뷰는 시장 구성과 분모가 달라 수치를 직접 비교할 수 없습니다"


def enforce_general_view_contract(answer: str, contract: dict[str, Any] | None) -> str:
    if not contract:
        return answer
    code = str(contract.get("atc4_code") or "")
    source = str(contract.get("source") or "")
    measure = str(contract.get("measure") or "")
    period = str(contract.get("period") or "")
    label = f"기준: 일반뷰 (ATC4 {code}) | 소스: {source} | 지표: {measure} | 기준: {period}"
    strategic_answer = answer.rstrip()
    if contract.get("mode") == "dual" and not strategic_answer.startswith("## 전략뷰 (market_landscape)"):
        strategic_answer = f"## 전략뷰 (market_landscape)\n\n{strategic_answer}"
    parts = [strategic_answer]
    section = str(contract.get("section_markdown") or "").strip()
    if section and section not in answer:
        parts.append(section)
    combined = "\n\n".join(part for part in parts if part)
    if label not in combined:
        combined = "\n\n".join(part for part in (combined, label) if part)
    if contract.get("mode") == "dual" and DUAL_WARNING not in combined:
        combined = "\n\n".join((combined, f"> {DUAL_WARNING}"))
    return combined
