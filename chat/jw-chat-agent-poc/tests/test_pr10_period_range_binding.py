from __future__ import annotations

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate


def _patient_fact(year: str, count: str) -> EvidenceFact:
    return EvidenceFact(
        fact_id=f"hira-d693-{year}",
        label=f"D69.3 {year} 환자수",
        value=count,
        source="HIRA",
        tool="hira_disease_hospitalization_outpatient_stats",
        path=f"items[{year}]",
        period=year,
        allowed_numbers=(count, f"{count}명"),
        entity="D69.3",
        metric="환자수",
        unit="명",
        source_grade="AUTHORITATIVE",
    )


def _binding_result(year_counts: tuple[tuple[str, str], ...]) -> dict[str, object]:
    return {
        "markdown_response": {
            "evidence": [
                _patient_fact(year, count).to_dict()
                for year, count in year_counts
            ]
        }
    }


def test_explicit_hira_year_range_keeps_every_requested_year() -> None:
    year_counts = (
        ("2021", "3663"),
        ("2022", "3633"),
        ("2023", "3757"),
        ("2024", "3620"),
    )
    answer = "\n".join(
        f"D69.3의 {year}년 환자수는 {count}명입니다."
        for year, count in year_counts
    )
    result = _binding_result(year_counts)

    gated = _apply_evidence_binding_gate(
        "D693 환자수 2021년부터 2024년까지 알려줘",
        answer,
        result,
    )

    assert gated == answer
    assert result["_qa_claim_gate"]["blocked_claim_count"] == 0
    assert result["_qa_claim_gate"]["blocked_reasons"] == ()


def test_recent_three_year_hira_answer_is_byte_unchanged() -> None:
    year_counts = (
        ("2022", "3633"),
        ("2023", "3757"),
        ("2024", "3620"),
    )
    answer = "\n".join(
        f"D69.3의 {year}년 환자수는 {count}명입니다."
        for year, count in year_counts
    )
    result = _binding_result(year_counts)

    gated = _apply_evidence_binding_gate(
        "D693 환자수 최근 3년 알려줘",
        answer,
        result,
    )

    assert gated == answer
    assert gated.encode() == answer.encode()
