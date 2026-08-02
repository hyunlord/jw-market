from __future__ import annotations

import logging
import os
from typing import Protocol

from jw_chat_agent_poc.common.timing import trace_span
from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationState,
    ConversationStore,
    ConversationTurn,
    PendingClarification,
)
from jw_chat_agent_poc.service.conversation_history import ConversationHistoryStore


LOGGER = logging.getLogger(__name__)

CONVERSATION_REPOSITORY_ENV = "JW_CHAT_CONVERSATION_REPOSITORY_ENABLED"


class ConversationRepository(Protocol):
    @property
    def pending_ttl_seconds(self) -> int: ...

    def get_or_create(self, conversation_id: str | None = None) -> ConversationState: ...

    def get_pending(self, conversation_id: str) -> PendingClarification | None: ...

    def set_pending(self, conversation_id: str, pending: PendingClarification) -> None: ...

    def clear_pending(self, conversation_id: str) -> None: ...

    def record_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        applied_filters: tuple[tuple[str, str], ...] = (),
        *,
        slots: ConversationSlots = ConversationSlots(),
    ) -> None: ...

    def pending_expiry(self) -> float: ...

    def observability(self) -> dict[str, int]: ...

    def hydrate_latest(self, conversation_id: str) -> bool: ...


class _ConversationRepositoryBase:
    def __init__(
        self,
        cache: ConversationStore,
        history: ConversationHistoryStore | None,
    ) -> None:
        self._cache = cache
        self._history = history

    @property
    def pending_ttl_seconds(self) -> int:
        return self._cache.pending_ttl_seconds

    def get_or_create(self, conversation_id: str | None = None) -> ConversationState:
        return self._cache.get_or_create(conversation_id)

    def get_pending(self, conversation_id: str) -> PendingClarification | None:
        return self._cache.get_pending(conversation_id)

    def set_pending(self, conversation_id: str, pending: PendingClarification) -> None:
        self._cache.set_pending(conversation_id, pending)

    def clear_pending(self, conversation_id: str) -> None:
        self._cache.clear_pending(conversation_id)

    def record_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        applied_filters: tuple[tuple[str, str], ...] = (),
        *,
        slots: ConversationSlots = ConversationSlots(),
    ) -> None:
        self._cache.record_exchange(
            conversation_id,
            question,
            answer,
            applied_filters,
            slots=slots,
        )

    def pending_expiry(self) -> float:
        return self._cache.pending_expiry()

    def observability(self) -> dict[str, int]:
        return self._cache.observability()

    def hydrate_latest(self, conversation_id: str) -> bool:
        latest_turn = getattr(self._history, "latest_turn", None)
        if not callable(latest_turn):
            return False
        try:
            with trace_span("conversation_history_fetch", "fetch latest persisted conversation turn"):
                turn = latest_turn(conversation_id)
        except Exception as exc:  # noqa: BLE001 - preserves the existing hydration fallback
            LOGGER.warning("conversation history hydration failed error_type=%s", type(exc).__name__)
            return False
        if not isinstance(turn, ConversationTurn):
            return False
        with trace_span("conversation_history_replay", "restore persisted turn into request state"):
            self.record_exchange(
                conversation_id,
                turn.question,
                turn.answer,
                turn.applied_filters,
                slots=turn.slots,
            )
        return True


class LegacyConversationRepository(_ConversationRepositoryBase):
    """Rollback adapter over the original cache and history implementations."""


class ExtractedConversationRepository(_ConversationRepositoryBase):
    """Persistence port used by the extracted conversation-state path."""


def conversation_repository_enabled() -> bool:
    return os.environ.get(CONVERSATION_REPOSITORY_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def build_conversation_repository(
    cache: ConversationStore,
    history: ConversationHistoryStore | None,
) -> ConversationRepository:
    repository_type = (
        ExtractedConversationRepository
        if conversation_repository_enabled()
        else LegacyConversationRepository
    )
    return repository_type(cache, history)
