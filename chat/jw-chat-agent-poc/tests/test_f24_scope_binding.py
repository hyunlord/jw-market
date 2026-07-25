"""F24 RC1 — scope-aware claim binding.

Reproduces the F21 live defect (case 08_topic_switch, "리바로 매출 알려줘",
disposition=partial, blocked_reason METRIC_MISMATCH): a numeric claim that is
valid in the requested scope gets wrongly excluded ("근거 불일치로 제외") because
the validator resolves the claim's metric from a *foreign-scope* table.

Two scopes are merged into one response under the same public 뷰 label ("전략뷰"):

    | 출처  | ... | 뷰      | 시장정의                  | 분모 |
    | UBIST | ... | 전략뷰  | 요청 브랜드의 전략 시장    | 555 |
    | UBIST | ... | 전략뷰  | 고지혈증                   | 566 |

The 555 scope carries the requested 리바로 매출 fact; the 566 scope is a
different market. Because scope was never compared, the same numeric token can be
metric-resolved against the wrong scope's table and the correct-scope fact is
dropped.

The RED test asserts the *desired* (post-fix) behaviour, so it fails on the
unfixed code and passes once scope-aware binding lands.
"""

from __future__ import annotations

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings


def _fact(
    *,
    fact_id: str,
    value: str,
    entity: str,
    metric: str,
    period: str,
    unit: str,
    view: str,
    source_grade: SourceGrade = SourceGrade.AUTHORITATIVE,
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id,
        label=metric,
        value=value,
        source="UBIST",
        tool="get_brand_metric",
        path=f"render_data.{metric}",
        period=period,
        allowed_numbers=(value,),
        entity=entity,
        metric=metric,
        unit=unit,
        source_grade=source_grade.value,
        view=view,
    )


# Public "전략뷰" hides two distinct internal market scopes (market_id stays internal).
_STRATEGIC_SCOPE = "market_landscape:ml_555"
_DISEASE_SCOPE = "market_landscape:ml_566"


def _scope_merged_answer() -> str:
    # The foreign 566/고지혈증 block is rendered *before* the requested 555 block,
    # and its 시장규모 header column happens to carry the same numeric token as the
    # requested 리바로 매출 value. This is the exact merge shape from the F21 output.
    return """### 고지혈증 시장 개요 (전략뷰)
| 시장정의 | 시장규모 |
| --- | --- |
| 고지혈증 | 80.39억원 |

### 리바로 지표 (요청 브랜드의 전략 시장, 전략뷰)
| 기준 브랜드 | 매출 |
| --- | --- |
| 리바로 | 80.39억원 |"""


def test_valid_sales_survives_when_a_foreign_scope_table_shares_the_number() -> None:
    """G-1: 리바로 2026-05 매출 must resolve to its real value, not be excluded.

    The correct-scope 매출 fact (80.39억원) is present; the only reason it is
    dropped on the unfixed code is that a foreign-scope (고지혈증) table hijacks
    the token's metric resolution to 시장규모, so entity∧metric binding fails.
    """
    facts = (
        _fact(
            fact_id="fact_sales_555",
            value="80.39억원",
            entity="리바로",
            metric="매출",
            period="2026-05",
            unit="억원",
            view=_STRATEGIC_SCOPE,
        ),
    )

    result = verify_claim_bindings(
        question="리바로 매출 알려줘",
        answer=_scope_merged_answer(),
        facts=facts,
        expected_entities=("리바로",),
    )

    # Desired: the valid 매출 is retained (not "근거 불일치로 제외"), disposition is
    # not a hard fail. On the unfixed code this is status="fail" /
    # METRIC_MISMATCH, so the assertions below reproduce the RED state.
    assert result.status != "fail", (
        f"valid 매출 wrongly excluded: status={result.status} "
        f"reasons={result.blocked_reasons}"
    )
    assert "80.39억원" in result.answer
    assert "METRIC_MISMATCH" not in result.blocked_reasons
    assert "80.39억원" not in _excluded_cells(result.answer)


def _excluded_cells(answer: str) -> str:
    """Return only the text of cells/lines that were replaced by the exclusion marker."""
    return "\n".join(line for line in answer.splitlines() if "근거 불일치로 제외" in line)
