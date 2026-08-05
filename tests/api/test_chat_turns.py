from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.scripts.api.chat_usage_materialization import (
    ChatMaterializationState,
    ChatMaterializationUnavailable,
)
from pipeline.scripts.api.dashboard_usage import (
    ChatTurnDetail,
    CHAT_TURNS_SQL,
    ChatTurnCursor,
    ChatTurnFilters,
    ChatTurnPage,
    MariaDBUsageRepository,
    decode_chat_turn_cursor,
    decode_chat_turn_id,
    encode_chat_turn_cursor,
    encode_chat_turn_id,
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
                    "turn_id": encode_chat_turn_id("trace-42"),
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
                ChatTurnCursor(datetime(2026, 8, 3, 9, 30), "market", "a" * 64)
            ),
            has_more=True,
            page=1,
            total_count=51,
            total_pages=2,
            page_size=50,
        )

    def fetch_chat_turn(self, detail_key: str) -> ChatTurnDetail | None:
        if detail_key != "trace-42":
            return None
        return ChatTurnDetail(
            item={
                "turn_id": encode_chat_turn_id(detail_key),
                "question_text": "질문",
                "answer_text": "# 안전한 답변",
            }
        )


def _client(
    repository: FakeChatTurnsRepository, *, raise_server_exceptions: bool = True
) -> TestClient:
    app = FastAPI()
    app.include_router(create_chat_turns_router(repository))
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_chat_turns_forwards_bounded_filters_and_opaque_cursor() -> None:
    repository = FakeChatTurnsRepository()
    cursor = encode_chat_turn_cursor(
        ChatTurnCursor(datetime(2026, 8, 3, 8, 0), "rnd", "b" * 64)
    )

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
            cursor=ChatTurnCursor(datetime(2026, 8, 3, 8, 0), "rnd", "b" * 64),
            page=1,
        )
    ]
    assert decode_chat_turn_cursor(response.json()["next_cursor"]) == ChatTurnCursor(
        datetime(2026, 8, 3, 9, 30), "market", "a" * 64
    )


def test_chat_turns_rejects_oversized_range_page_and_tampered_cursor() -> None:
    client = _client(FakeChatTurnsRepository())

    oversized_range = client.get(
        "/api/dashboard/chat-turns?date_from=2026-07-01&date_to=2026-08-03"
    )
    oversized_page = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&page_size=101"
    )
    legacy_page_size = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&page_size=25"
    )
    bad_cursor = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&cursor=bad"
    )
    mixed_page_cursor = client.get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03&page=2"
        "&cursor="
        + encode_chat_turn_cursor(ChatTurnCursor(datetime(2026, 8, 3, 8, 0), "rnd", "b" * 64))
    )

    assert oversized_range.status_code == 422
    assert oversized_page.status_code == 422
    assert legacy_page_size.status_code == 200
    assert bad_cursor.status_code == 400
    assert mixed_page_cursor.status_code == 422


def test_chat_turn_items_are_metadata_only_and_never_expose_unlinked_or_raw_fields() -> None:
    response = _client(FakeChatTurnsRepository()).get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03"
    )
    payload = response.json()

    assert response.status_code == 200
    assert set(payload) == {
        "items",
        "next_cursor",
        "has_more",
        "page",
        "total_count",
        "total_pages",
        "page_size",
    }
    assert set(payload["items"][0]) == {
        "turn_id",
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


def test_chat_turn_detail_is_trace_backed_and_separate_from_list() -> None:
    turn_id = encode_chat_turn_id("trace-42")

    response = _client(FakeChatTurnsRepository()).get(f"/api/dashboard/chat-turns/{turn_id}")
    missing = _client(FakeChatTurnsRepository()).get(
        f"/api/dashboard/chat-turns/{encode_chat_turn_id('trace-missing')}"
    )
    invalid = _client(FakeChatTurnsRepository()).get("/api/dashboard/chat-turns/t1_legacy")

    assert decode_chat_turn_id(turn_id) == "trace-42"
    assert response.status_code == 200
    assert set(response.json()) == {"turn_id", "question_text", "answer_text"}
    assert response.json()["answer_text"] == "# 안전한 답변"
    assert missing.status_code == 404
    assert invalid.status_code == 400
    assert "answer_text" not in _client(FakeChatTurnsRepository()).get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03"
    ).text


def test_chat_turn_query_is_sanitized_linked_and_keyset_ordered() -> None:
    normalized = " ".join(CHAT_TURNS_SQL.split()).lower()

    assert "from dashboard_chat_usage_v c" in normalized
    assert "join dashboard_user_directory_v u" in normalized
    assert "c.service_id in (61, 91, 94)" in normalized
    assert "c.portal_user_id is not null" in normalized
    assert "as source" in normalized
    assert "as source_turn_id" in normalized
    assert "order by c.created_at desc, source desc, source_turn_id desc" in normalized
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


def test_chat_turns_accepts_numbered_pagination_metadata() -> None:
    repository = FakeChatTurnsRepository()
    response = _client(repository).get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03"
        "&page=3&page_size=10"
    )

    assert response.status_code == 200
    assert repository.calls[-1].page == 3
    assert repository.calls[-1].page_size == 10
    assert response.json()["page"] == 1
    assert response.json()["total_pages"] == 2


def test_chat_turn_repository_pages_past_nullable_market_identity() -> None:
    base = datetime(2026, 8, 3, 12, 0)
    rows = []
    for index in range(51):
        is_rnd = index >= 23
        rows.append(
            {
                "conversation_log_id": None if is_rnd else 1000 - index,
                "created_at": base - timedelta(seconds=index),
                "service_id": 61 if is_rnd else 91,
                "user_id": 34,
                "user_name": "display name",
                "department": "Market",
                "conversation_id": f"session-{index}",
                "turn_index": index + 1,
                "trace_id": f"trace-{index}",
                "contract_status": "complete",
                "quality_label": "rnd_trace" if is_rnd else "na",
                "elapsed_ms": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "source": "rnd" if is_rnd else "market",
                "source_turn_id": f"{index:064x}",
            }
        )

    class Cursor:
        executed: list[tuple[str, tuple]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            self.executed.append((sql, params))

        def fetchall(self):
            return rows

        def fetchone(self):
            return {"total_count": len(rows)}

    class Connection:
        def __init__(self):
            self.db_cursor = Cursor()
            self.closed = False

        def cursor(self):
            return self.db_cursor

        def close(self):
            self.closed = True

    connection = Connection()
    config = SimpleNamespace(
        dashboard_db_host="db",
        dashboard_db_port=3306,
        dashboard_db_user="reader",
        dashboard_db_password="not-recorded",
        dashboard_db_name="audit",
    )
    repository = MariaDBUsageRepository(config, connect=lambda **_kwargs: connection)
    repository._fetch_chat_materialization_state = lambda: ChatMaterializationState(  # type: ignore[method-assign]
        date(2026, 7, 9),
        date(2026, 8, 4),
        datetime.now(UTC),
        "complete",
    )

    page = repository.fetch_chat_turns(
        ChatTurnFilters(date(2026, 8, 1), date(2026, 8, 3), page_size=50)
    )

    assert page.has_more is True
    assert len(page.items) == 50
    assert page.total_count == 51
    assert page.total_pages == 2
    assert connection.closed is True
    cursor = decode_chat_turn_cursor(page.next_cursor or "")
    assert cursor.source == "rnd"
    assert cursor.source_turn_id == f"{49:064x}"


def test_chat_turns_maps_unexpected_repository_failure_to_json() -> None:
    class BrokenRepository(FakeChatTurnsRepository):
        def fetch_chat_turns(self, filters: ChatTurnFilters) -> ChatTurnPage:
            raise TypeError("nullable source identity")

    response = _client(BrokenRepository(), raise_server_exceptions=False).get(
        "/api/dashboard/chat-turns?date_from=2026-08-01&date_to=2026-08-03"
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["error"] == "chat_turns_internal_error"
