from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator import market_answer_contract
from jw_chat_agent_poc.orchestrator.market_answer_contract import _complete_row
from jw_chat_agent_poc.orchestrator.provenance_facts import provenance_rows_from_fact_markdown
from jw_chat_agent_poc.orchestrator.provenance_model import ProvenanceRow


@pytest.mark.parametrize(
    ("brand", "market"),
    (
        ("브랜드A", "고지혈증"),
        ("브랜드B", "당뇨병"),
        ("브랜드C", "안과질환"),
    ),
)
def test_complete_row_restores_each_public_market_from_strategic_view(
    brand: str,
    market: str,
) -> None:
    # Given
    row = ProvenanceRow(
        source="UBIST",
        view=f"전략뷰 (market_landscape) · {market}",
        brand=brand,
    )

    # When
    completed = _complete_row(row, question="브랜드별 매출", answer="")

    # Then
    assert completed.view == "전략뷰"
    assert completed.market == market
    assert completed.brand == brand


def test_complete_row_keeps_common_verified_market_for_two_brands() -> None:
    # Given
    rows = (
        ProvenanceRow(view="전략뷰 (market_landscape) · 고지혈증", brand="리바로"),
        ProvenanceRow(view="전략뷰 (market_landscape) · 고지혈증", brand="로수젯"),
    )

    # When
    completed = tuple(_complete_row(row, question="두 브랜드 매출", answer="") for row in rows)

    # Then
    assert tuple(row.market for row in completed) == ("고지혈증", "고지혈증")


def test_complete_row_keeps_generic_literal_when_market_is_truly_missing() -> None:
    # Given
    row = ProvenanceRow(view="전략뷰 (market_landscape)", brand="리바로")

    # When
    completed = _complete_row(row, question="리바로 매출", answer="")

    # Then
    assert completed.market == "요청 브랜드의 전략 시장"


def test_complete_row_does_not_expose_internal_market_id_from_view() -> None:
    # Given
    row = ProvenanceRow(view="전략뷰 (market_landscape) · ml_006", brand="리바로")

    # When
    completed = _complete_row(row, question="리바로 매출", answer="")

    # Then
    assert completed.market == "요청 브랜드의 전략 시장"
    assert "ml_006" not in completed.as_tuple()


def test_legacy_seven_column_row_keeps_its_market_definition() -> None:
    # Given
    fact = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-05 | 전략뷰 (market_landscape) | 고지혈증 | 555 | 전체 | 억원 |
"""

    # When
    (parsed,) = provenance_rows_from_fact_markdown(fact)
    completed = _complete_row(parsed, question="시장 매출", answer="")

    # Then
    assert completed.market == "고지혈증"
    assert completed.brand == "해당 없음"


def test_current_eight_column_row_keeps_market_and_brand() -> None:
    # Given
    fact = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 | 브랜드 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IQVIA NSA | 2026-Q1 | 전략뷰 (market_landscape) | 안과질환 | 9 | 전체 | 억원 | 아일리아 |
"""

    # When
    (parsed,) = provenance_rows_from_fact_markdown(fact)
    completed = _complete_row(parsed, question="아일리아 매출", answer="")

    # Then
    assert completed.market == "안과질환"
    assert completed.brand == "아일리아"


def test_answer_market_is_not_injected_across_different_verified_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    rows = (
        ProvenanceRow(
            source="UBIST",
            view="전략뷰 (market_landscape) · 고지혈증",
            brand="리바로",
        ),
        ProvenanceRow(
            source="UBIST",
            view="전략뷰 (market_landscape) · 당뇨병",
            brand="가드렛",
        ),
        ProvenanceRow(
            source="UBIST",
            view="전략뷰 (market_landscape)",
            brand="시장미확인브랜드",
        ),
    )
    monkeypatch.setattr(market_answer_contract, "_provenance_rows", lambda _calls: rows)

    # When
    answer = market_answer_contract._replace_provenance(
        "브랜드별 매출",
        "요약\n- 시장: 고지혈증",
        ({"tool": "get_brand_metric"},),
    )

    # Then
    assert "| 전략뷰 | 고지혈증 |" in answer
    assert "| 전략뷰 | 당뇨병 |" in answer
    assert "| 전략뷰 | 요청 브랜드의 전략 시장 |" in answer


def test_answer_market_applies_to_single_row() -> None:
    # Given
    rows = (ProvenanceRow(view="전략뷰 (market_landscape)", brand="리바로"),)

    # When / Then
    assert market_answer_contract._answer_market_applies_to_rows(rows) is True


def test_answer_market_applies_when_all_verified_markets_are_identical() -> None:
    # Given
    rows = (
        ProvenanceRow(view="전략뷰 (market_landscape) · 고지혈증", brand="리바로"),
        ProvenanceRow(view="전략뷰 (market_landscape) · 고지혈증", brand="로수젯"),
    )

    # When / Then
    assert market_answer_contract._answer_market_applies_to_rows(rows) is True
