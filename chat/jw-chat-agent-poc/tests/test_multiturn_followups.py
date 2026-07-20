from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn
from jw_chat_agent_poc.service.conversation_context import requires_previous_turn, resolve_anaphora
from jw_chat_agent_poc.service import file_sql_query


def _turn(
    question: str,
    *,
    brand: str | None = None,
    market: str | None = None,
    market_definition: str | None = None,
    period: str | None = None,
    metric: str | None = None,
    view: str | None = None,
    file_name: str | None = None,
    file_measure: str | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        question=question,
        answer="verified previous answer",
        slots=ConversationSlots(
            anchor_brand=brand,
            market=market,
            market_definition=market_definition,
            period=period,
            metric=metric,
            view=view,
            file_name=file_name,
            file_measure=file_measure,
        ),
    )


@pytest.mark.parametrize(
    ("question", "previous", "expected"),
    [
        (
            "그 전 해는?",
            _turn("2024년은?", brand="리바로", period="2026-05", metric="매출"),
            "리바로 2023년 매출은?",
        ),
        (
            "걔 최근 추세는?",
            _turn("그중 1위 점유율은?", brand="로수젯", metric="점유율"),
            "로수젯 최근 점유율 추세는?",
        ),
        (
            "리바로젯은?",
            _turn("리바로 매출", brand="리바로", period="2026-05", metric="매출"),
            "리바로젯 매출은?",
        ),
        (
            "시장 규모는?",
            _turn(
                "고지혈증 시장 HHI",
                market="ml_006",
                market_definition="고지혈증 시장",
                metric="HHI",
            ),
            "고지혈증 시장 규모는?",
        ),
        (
            "일반뷰로는?",
            _turn(
                "리바로 전략뷰 시장 규모",
                brand="리바로",
                market="ml_006",
                metric="시장 규모",
                view="strategic_ml",
            ),
            "리바로 일반뷰 시장 규모는?",
        ),
        (
            "비만 시장에서는?",
            _turn(
                "고지혈증 시장에서 마운자로 점유율",
                brand="마운자로",
                market="ml_006",
                market_definition="고지혈증 시장",
                metric="점유율",
            ),
            "비만 시장에서 마운자로 점유율은?",
        ),
    ],
)
def test_deterministic_followups_inherit_only_the_requested_slot(
    question: str,
    previous: ConversationTurn,
    expected: str,
) -> None:
    assert requires_previous_turn(question) is True

    resolved = resolve_anaphora(question, previous)

    assert resolved.resolved_question == expected
    assert resolved.unresolved_reference is False


@pytest.mark.parametrize(
    "question",
    [
        "그 전 해는?",
        "걔 최근 추세는?",
        "시장 규모는?",
        "일반뷰로는?",
        "비만 시장에서는?",
    ],
)
def test_deterministic_followups_fail_closed_without_previous_slots(question: str) -> None:
    assert requires_previous_turn(question) is True
    assert resolve_anaphora(question, None).unresolved_reference is True


def test_unrelated_new_topic_does_not_inherit_previous_slots() -> None:
    previous = _turn("리바로 매출", brand="리바로", metric="매출")

    resolved = resolve_anaphora("오늘 날씨 어때", previous)

    assert requires_previous_turn("오늘 날씨 어때") is False
    assert resolved.resolved_question == "오늘 날씨 어때"
    assert resolved.unresolved_reference is False


def test_file_channel_followup_requires_history_and_adds_exact_channel_filter() -> None:
    schema = {
        "logical_name": "doc-1:sheet-1",
        "file_name": "channels.xlsx",
        "sheet_name": "data",
        "columns": [
            {"query_name": "c1", "source_name": "CHANNEL"},
        ],
    }

    assert requires_previous_turn("그중 1번 채널은 몇 건이야?") is True

    resolution = file_sql_query._resolve_deterministic_select(
        "channels.xlsx에서 그중 1번 채널은 몇 건이야?",
        (schema,),
    )

    assert resolution.plan is not None
    assert "c1 = '1'" in resolution.plan["sql"]
    assert "COUNT(*) AS response_count" in resolution.plan["sql"]
    assert resolution.missing_slots == ()
