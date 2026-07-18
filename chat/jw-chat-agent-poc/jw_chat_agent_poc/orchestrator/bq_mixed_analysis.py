from __future__ import annotations

from typing import Any, Final

from jw_chat_agent_poc.orchestrator.bq_evidence_ledger import build_evidence_ledger


Call = dict[str, Any]
FILE_MARKET_COMPARISON_CONTRACT = "FILE_MARKET_COMPARISON"
MARKET_SOURCE_LABELS: Final = frozenset({"UBIST", "IQVIA NSA"})
_FILE_EVIDENCE_REF = "FILE.deterministic_answer"


def build_file_market_analysis_call(
    market_calls: list[Call],
    deterministic_file_answer: str,
) -> Call | None:
    file_answer = deterministic_file_answer.strip()
    market_ledger = build_evidence_ledger(market_calls)
    market_sources = list(
        dict.fromkeys(
            str(row.get("source") or "").strip()
            for row in market_ledger
            if str(row.get("source") or "").strip() in MARKET_SOURCE_LABELS
        )
    )
    market_references = list(
        dict.fromkeys(
            str(reference).strip()
            for row in market_ledger
            for reference in row.get("references", [])
            if str(reference).strip()
        )
    )
    if not file_answer or not market_ledger or not market_sources or not market_references:
        return None

    insight = (
        "업로드 문서 결과와 시장 지표를 출처별로 나란히 제시하며 "
        "기준기간·단위·정의가 다를 수 있어 합산하지 않습니다."
    )
    file_row = {
        "source": "FILE",
        "kind": "file_answer",
        "identity": "uploaded_file:deterministic_answer",
        "value": file_answer,
        "references": [_FILE_EVIDENCE_REF],
    }
    return {
        "source": "BQ deterministic evidence",
        "tool": "bq_analysis",
        "summary_text": insight,
        "render_data": {
            "contract_id": FILE_MARKET_COMPARISON_CONTRACT,
            "calculation": "side_by_side_file_market_comparison",
            "insights": [insight],
            "source_labels": ["FILE", *market_sources],
            "market_source_labels": market_sources,
            "never_aggregate_sources": True,
            "fusion_mode": "side_by_side",
            "evidence_refs": [_FILE_EVIDENCE_REF, *market_references],
            "evidence_ledger": [file_row, *market_ledger],
        },
    }
