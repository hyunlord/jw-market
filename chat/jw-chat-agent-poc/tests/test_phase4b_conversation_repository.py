from __future__ import annotations

import pytest

from jw_chat_agent_poc.service import conversation_repository
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationStore,
    ConversationTurn,
)


class _History:
    def __init__(self, turn: ConversationTurn | None = None) -> None:
        self.turn = turn
        self.requested_ids: list[str] = []

    def latest_turn(self, conversation_id: str) -> ConversationTurn | None:
        self.requested_ids.append(conversation_id)
        return self.turn


def test_repository_hydrates_persisted_turn_into_empty_cache(monkeypatch) -> None:
    monkeypatch.setenv(conversation_repository.CONVERSATION_REPOSITORY_ENV, "1")
    cache = ConversationStore()
    persisted = ConversationTurn(
        question="리바로 2025년 매출",
        answer="확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", period="2025"),
    )
    history = _History(persisted)
    repository = conversation_repository.build_conversation_repository(cache, history)

    assert repository.hydrate_latest("cross-pod") is True
    state = repository.get_or_create("cross-pod")

    assert history.requested_ids == ["cross-pod"]
    assert state.turns == (persisted,)


def test_repository_cache_hit_does_not_require_history() -> None:
    cache = ConversationStore()
    cache.record_exchange(
        "local",
        "리바로 매출",
        "확인했습니다.",
        (),
        slots=ConversationSlots(anchor_brand="리바로"),
    )
    history = _History()
    repository = conversation_repository.build_conversation_repository(cache, history)

    assert repository.get_or_create("local").turns[0].slots.anchor_brand == "리바로"
    assert history.requested_ids == []


def test_flag_off_never_constructs_extracted_repository(monkeypatch) -> None:
    monkeypatch.setenv(conversation_repository.CONVERSATION_REPOSITORY_ENV, "0")

    class UnexpectedRepository:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("extracted repository path must remain disabled")

    monkeypatch.setattr(
        conversation_repository,
        "ExtractedConversationRepository",
        UnexpectedRepository,
    )

    repository = conversation_repository.build_conversation_repository(
        ConversationStore(),
        _History(),
    )

    assert isinstance(repository, conversation_repository.LegacyConversationRepository)


def test_session_store_flag_off_never_uses_extracted_repository(monkeypatch) -> None:
    monkeypatch.setenv(conversation_repository.CONVERSATION_REPOSITORY_ENV, "0")

    class UnexpectedRepository:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("extracted repository path must remain disabled")

    monkeypatch.setattr(
        conversation_repository,
        "ExtractedConversationRepository",
        UnexpectedRepository,
    )

    store = SessionStore()
    store.configure_conversation_repository(_History())

    assert isinstance(
        store.conversations,
        conversation_repository.LegacyConversationRepository,
    )


def test_hydration_failure_keeps_existing_fail_open_behavior(monkeypatch) -> None:
    monkeypatch.setenv(conversation_repository.CONVERSATION_REPOSITORY_ENV, "1")

    class BrokenHistory:
        def latest_turn(self, _conversation_id: str) -> ConversationTurn | None:
            raise RuntimeError("database unavailable")

    repository = conversation_repository.build_conversation_repository(
        ConversationStore(),
        BrokenHistory(),
    )

    assert repository.hydrate_latest("cross-pod") is False
    assert repository.get_or_create("cross-pod").turns == ()


def test_hydration_preserves_invalid_turn_rejection() -> None:
    class InvalidHistory:
        def latest_turn(self, _conversation_id: str) -> object:
            return {"question": "not a ConversationTurn"}

    repository = conversation_repository.build_conversation_repository(
        ConversationStore(),
        InvalidHistory(),  # type: ignore[arg-type]
    )

    assert repository.hydrate_latest("cross-pod") is False
    assert repository.get_or_create("cross-pod").turns == ()


@pytest.mark.parametrize("enabled", ("0", "1"))
def test_repository_delegates_pending_state_without_changing_ttl(monkeypatch, enabled: str) -> None:
    monkeypatch.setenv(conversation_repository.CONVERSATION_REPOSITORY_ENV, enabled)
    cache = ConversationStore(pending_ttl_seconds=37)
    repository = conversation_repository.build_conversation_repository(cache, _History())

    assert repository.pending_ttl_seconds == 37
    assert repository.observability() == cache.observability()
