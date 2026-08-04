from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.scripts.api.chat_usage_materialization import (
    ChatMaterializationState,
    ChatMaterializationUnavailable,
)
from pipeline.scripts.api.dashboard_usage import (
    CHAT_TURNS_SQL,
    ChatTurnCursor,
    ChatTurnFilters,
    ChatTurnPage,
    decode_chat_turn_cursor,
    encode_chat_turn_cursor,
)
from pipeline.scripts.api.routes.dashboard_usage import create_chat_turns_router


class FakeChatTurnsRepository:
    def __init__(self) -> None:
        self.calls: list[ChatTurnFilters] = []

    def fetch_chat_turns(self, filters: ChatTurnFilters) -> ChatTurnPage:
        self.calls.append(filters)
        return ChatTurnPage(
            items=(
                {
                    "user_id": 34,
                    "user_name": "display name",
                    "department": "Market",
                    "created_at": datetime(2026, 8, 3, 9, 30),
                    "service_id": 91,
                    "service_category": "market",
                    "turn_index": 2,
                    "contract_status": "PASS",
                    "quality_label": "SUPPORTED",
                    "elapsed_ms": 3210,
                    "input_tokens": 120,
                    "output_tokens": 240,
                    "total_tokens": 360,
                },
            ),
            next_cursor=encode_chat_turn_cursor(
                ChatTurnCursor(datetime(2026, 8, 3, 9, 30), 42)
            ),
            has_more=True,
        )


def _client(repository: FakeChatTurnsRepository) -> TestClient:
    app = FastAPI()
    app.include_router(create_chat_turns_router(repository))
    return TestClient(app)


def test_chat_turns_forwards_bounded_filters_and_opaque_cursor() -> None:
    repository = FakeChatTurnsRepository()
    cursor = encode_chat_turn_cursor(ChatTurnCursor(datetime(2026, 8, 3, 8, 0), 41))

    response = _client(repository).get(
        "/api/dashboard/chat-turns",
        params={
            "date_from": "2026-08-01",
            "date_to": "2026-08-03",
            "user_id": 34,
            "user_ids": [34, 35],
            "excluded_user_ids": [82, 85],
            "department": "Market",
            "page_size": 100,
            "cursor": cursor,
        },
    )

    assert response.status_code == 200
    assert repository.calls == [
        ChatTurnFilters(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 3),
            user_id=34,
            user_ids=(34, 35),
            excluded_user_ids=(82, 85),
            department="Market",
            page_size=100,
            cursor=ChatTurnCursor(datetime(2026, 8, 3, 8, 0), 41),
        )
    ]
    assert decode_chat_turn_cursor(response.json()["next_cursor"]) == ChatTurnCursor(
        datetime(2026, 8, 3, 9, 30), 42
    )


def test_chat_turns_rejects_oversized_range_page_and_tampered_cursor() -> None:
    client = _client(FakeChatTurnsRepository())

    oversized_range = client.get(
        "/api/dashboard/chat-turns?date_from=2026-07-01&date_to=2026-08-03"
    )
    oversized_page = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&page_size=101"
    )
    bad_cursor = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&cursor=bad"
    )

    assert oversized_range.status_code == 422
    assert oversized_page.status_code == 422
    assert bad_cursor.status_code == 400


def test_chat_turn_items_are_metadata_only_and_never_expose_unlinked_or_raw_fields() -> None:
    response = _client(FakeChatTurnsRepository()).get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03"
    )
    payload = response.json()

    assert response.status_code == 200
    assert set(payload) == {"items", "next_cursor", "has_more"}
    assert set(payload["items"][0]) == {
        "user_id",
        "user_name",
        "department",
        "created_at",
        "service_id",
        "service_category",
        "turn_index",
        "contract_status",
        "quality_label",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
    serialized = response.text.lower()
    for forbidden in ("question", "answer", "email", "actor_uid", "request_params", "jti"):
        assert forbidden not in serialized


def test_chat_turn_query_is_sanitized_linked_and_keyset_ordered() -> None:
    normalized = " ".join(CHAT_TURNS_SQL.split()).lower()

    assert "from dashboard_chat_usage_v c" in normalized
    assert "join dashboard_user_directory_v u" in normalized
    assert "c.service_id in (61, 91, 94)" in normalized
    assert "c.portal_user_id is not null" in normalized
    assert "order by c.created_at desc, c.conversation_log_id desc" in normalized
    for forbidden in ("question", "answer", "email", "actor_uid", "request_params", "jti"):
        assert forbidden not in normalized


def test_chat_turns_maps_coverage_failure_to_structured_503() -> None:
    class UnavailableRepository(FakeChatTurnsRepository):
        def fetch_chat_turns(self, filters: ChatTurnFilters) -> ChatTurnPage:
            state = ChatMaterializationState(
                date(2026, 7, 9),
                date(2026, 8, 4),
                datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
                "complete",
            )
            raise ChatMaterializationUnavailable(
                "outside coverage", reason="coverage", state=state
            )

    response = _client(UnavailableRepository()).get(
        "/api/dashboard/chat-turns?date_from=2026-07-08&date_to=2026-08-03"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "chat_materialization_unavailable",
        "reason": "coverage",
        "message": "요청한 기간의 채팅 통계가 아직 준비되지 않았습니다.",
        "available_from": "2026-07-09",
        "available_to": "2026-08-03",
    }
    assert response.json()["limits"] == {"max_days": 31, "cache_ttl_seconds": 60}
