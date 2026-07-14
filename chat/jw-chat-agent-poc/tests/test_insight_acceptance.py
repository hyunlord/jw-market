from __future__ import annotations

from jw_chat_agent_poc.orchestrator.insight_acceptance import verify_insight_answer
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact


def test_acceptance_format_and_normal_answer_exit_zero() -> None:
    result = verify_insight_answer(
        gate="G4",
        markdown="CR5 29.52%",
        facts=(_boundary_fact(),),
        environment="local-test",
    )

    assert result.exit_code == 0
    assert result.failures == ()
    assert result.to_text().splitlines() == [
        "gate=G4",
        "classification=census",
        "checked=1",
        "population=1",
        "missing=fail",
        "tolerance=exact",
        "failures=0",
        "exit_code=0",
        "environment=local-test",
    ]


def test_numeric_forbidden_rounding_and_empty_population_injections_exit_one() -> None:
    fact = _boundary_fact()
    injections = (
        verify_insight_answer(gate="I1", markdown="CR5 999.99%", facts=(fact,)),
        verify_insight_answer(gate="I2", markdown="경쟁 심화로 CR5 29.52%", facts=(fact,)),
        verify_insight_answer(gate="I5", markdown="확인 불가", facts=()),
        verify_insight_answer(gate="I6", markdown="CR5 29.53%", facts=(fact,)),
    )

    assert all(result.exit_code == 1 for result in injections)
    assert all(result.failures for result in injections)


def _boundary_fact() -> EvidenceFact:
    return EvidenceFact(
        fact_id="cr5",
        label="CR5",
        value="29.52%",
        source="UBIST",
        tool="get_top_brands",
        path="render_data.series_insight.cr5_end_pct",
        period="2026-05",
        allowed_numbers=("CR5", "29.52%"),
    )
