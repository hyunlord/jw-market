from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import ValidationError

from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.renderer import render_evidence_claim
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    ReimbursementLookupResult,
)

_REIMBURSEMENT_TOOL: Final[str] = "hira_reimbursement_criteria"


def reimbursement_envelope(
    result: ReimbursementLookupResult,
    *,
    subject: str,
) -> ToolEnvelope:
    if not result.ok or result.data is None:
        error_code = result.error_code or "NO_EVIDENCE"
        message = {
            "TOOL_TIMEOUT": "HIRA 급여기준 실시간 조회 시간이 초과되었습니다.",
            "NO_EVIDENCE": "HIRA 보험인정기준에서 해당 제품의 기록을 찾지 못했습니다.",
        }.get(error_code, "HIRA 급여기준 원천을 확인할 수 없습니다.")
        return ToolEnvelope(
            ok=False,
            preview=message,
            evidence=(),
            raw={"retrieval": result.retrieval, "cache_status": result.cache_status.value},
            error_code=error_code,
            error_message=message,
        )

    data = result.data
    fact = EvidenceFact(
        fact_id=f"hira_reimbursement:{subject}:{data.source_date or 'undated'}",
        subject=subject,
        metric="HIRA 보험인정기준 원문 (AI 요약·해석·재구성 없음)",
        value=None,
        unit=None,
        period=data.source_date,
        source_name="심사평가원(HIRA) 보험인정기준",
        source_locator=data.raw_text,
        raw_ref=data.source_url,
    )
    return ToolEnvelope(
        ok=True,
        preview=f"{subject} HIRA 보험인정기준 원문 확인 (AI 요약·해석·재구성 없음)",
        evidence=(fact,),
        raw={
            "retrieval": result.retrieval,
            "cache_status": result.cache_status.value,
            "cache_write": result.cache_write,
            "notice_number": data.notice_number,
            "source_url": data.source_url,
        },
        error_code=None,
        error_message=None,
    )


def project_reimbursement_evidence(
    tool_calls: Sequence[Mapping[str, Any]],
    fact_md: str,
) -> list[dict[str, Any]]:
    """Project rendered HIRA criteria facts beside the existing F4 projection."""

    rendered_lines = frozenset(fact_md.splitlines())
    projected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for call in tool_calls:
        if call.get("tool") != _REIMBURSEMENT_TOOL or call.get("status") != "ok":
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
            continue
        serialized = render_data.get("evidence")
        if not isinstance(serialized, Sequence) or isinstance(serialized, str | bytes):
            continue
        for raw_fact in serialized:
            if not isinstance(raw_fact, Mapping):
                continue
            try:
                fact = EvidenceFact.model_validate(raw_fact)
            except ValidationError:
                continue
            rendered = render_evidence_claim(fact)
            if (
                fact.fact_id in seen_ids
                or rendered not in rendered_lines
                or not (fact.source_locator or "").strip()
            ):
                continue
            seen_ids.add(fact.fact_id)
            projected.append(_legacy_reimbursement_fact(fact, rendered))
    return projected


def _legacy_reimbursement_fact(
    fact: EvidenceFact,
    rendered: str,
) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "label": fact.metric,
        "value": str(fact.source_locator or ""),
        "source": fact.source_name,
        "tool": _REIMBURSEMENT_TOOL,
        "path": fact.raw_ref or f"render_data.evidence.{fact.fact_id}",
        "period": fact.period or "",
        "allowed_numbers": list(allowed_numbers(rendered)),
        "visible": True,
        "entity": fact.subject,
        "metric": fact.metric,
        "unit": fact.unit or "",
        "source_grade": "AUTHORITATIVE",
        "view": "",
        "operand_fact_ids": [],
    }
