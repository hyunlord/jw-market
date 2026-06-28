from __future__ import annotations

from jw_chat_agent_poc.service.conversation import ConversationStore, PendingClarification


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_conversation_store_keeps_recent_five_exchanges() -> None:
    clock = Clock()
    store = ConversationStore(max_turns=5, ttl_seconds=60, pending_ttl_seconds=30, clock=clock)
    state = store.get_or_create("conv-1")

    for index in range(7):
        store.record_exchange(state.conversation_id, f"질문 {index}", f"답변 {index}", (("metric", "sales"),))

    state = store.get_or_create("conv-1")
    assert [turn.question for turn in state.turns] == ["질문 2", "질문 3", "질문 4", "질문 5", "질문 6"]


def test_conversation_store_expires_conversation_and_pending() -> None:
    clock = Clock()
    store = ConversationStore(max_turns=5, ttl_seconds=10, pending_ttl_seconds=3, clock=clock)
    state = store.get_or_create("conv-1")
    pending = PendingClarification(
        kind="market_view",
        original_question="리바로랑 같은 시장 매출은 어느 기준?",
        brand="리바로",
        metric="sales",
        created_at=clock(),
        expires_at=clock() + 3,
    )
    store.set_pending(state.conversation_id, pending)

    assert store.get_pending("conv-1") == pending

    clock.now += 4
    assert store.get_pending("conv-1") is None

    clock.now += 7
    refreshed = store.get_or_create("conv-1")
    assert refreshed.conversation_id == "conv-1"
    assert refreshed.turns == ()
    assert refreshed.pending is None
