from __future__ import annotations

from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationStore, PendingClarification


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


def test_conversation_store_preserves_structured_slots_with_turn() -> None:
    store = ConversationStore()
    slots = ConversationSlots(anchor_brand="리바로", ranked_brands=("로수젯", "리피토", "리바로"))

    store.record_exchange("conv-slots", "상위 3개", "답변", slots=slots)

    assert store.get_or_create("conv-slots").turns[-1].slots == slots


def test_conversation_store_caps_distinct_ids_and_evicts_lru() -> None:
    store = ConversationStore(max_states=3)
    for conversation_id in ("conv-a", "conv-b", "conv-c"):
        store.record_exchange(conversation_id, conversation_id, "answer")

    store.get_or_create("conv-a")
    store.get_or_create("conv-d")

    metrics = store.observability()
    assert metrics["state_count"] == 3
    assert metrics["max_states"] == 3
    assert metrics["capacity_evictions"] == 1
    assert store.get_or_create("conv-b").turns == ()


def test_conversation_store_sweeps_expired_ids_without_accessing_them() -> None:
    clock = Clock()
    store = ConversationStore(max_states=3, ttl_seconds=10, clock=clock)
    store.record_exchange("expired", "question", "answer")

    clock.now += 11
    store.get_or_create("current")

    metrics = store.observability()
    assert metrics["state_count"] == 1
    assert metrics["ttl_evictions"] == 1


def test_conversation_store_sweeps_expired_id_even_when_lru_order_differs_from_ttl() -> None:
    clock = Clock()
    store = ConversationStore(max_states=3, ttl_seconds=10, clock=clock)
    store.record_exchange("expires-first", "question", "answer")
    clock.now += 8
    store.record_exchange("fresh", "question", "answer")
    store.get_or_create("expires-first")

    clock.now += 3
    store.get_or_create("current")

    metrics = store.observability()
    assert metrics["state_count"] == 2
    assert metrics["ttl_evictions"] == 1


def test_conversation_store_observability_is_aggregate_only() -> None:
    store = ConversationStore(max_states=3)
    store.record_exchange("secret-conversation", "private question", "private answer")

    metrics = store.observability()
    rendered = repr(metrics)

    assert metrics["state_count"] == 1
    assert metrics["turn_count"] == 1
    assert metrics["approx_bytes"] > 0
    assert "secret-conversation" not in rendered
    assert "private question" not in rendered
    assert "private answer" not in rendered
