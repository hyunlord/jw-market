from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from jw_chat_agent_poc.service.app import SessionStore, create_app
from jw_chat_agent_poc.service.concurrency import BUSY_MESSAGE, ChatBusyError, ChatConcurrencyLimiter
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CacheSnapshot,
    CausePayload,
    CausePayloadKey,
    TtlCausePayloadCache,
    TtlMetricsCache,
    shared_cause_payload_cache,
    shared_metrics_cache,
)


def _cause_key(brand: str) -> CausePayloadKey:
    return CausePayloadKey(brand=brand, view_type="market_landscape", source="UBIST", measure="sales", market_id="strategy_006")


class CountingSnapshotReader:
    def __init__(self, delay_s: float = 0.2) -> None:
        self.calls = 0
        self.delay_s = delay_s
        self._lock = threading.Lock()

    def load(self) -> CacheSnapshot:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay_s)
        return CacheSnapshot(cache_brands=[], market_status={}, loaded_at=time.monotonic())


class CountingCauseReader:
    def __init__(self, delay_s: float = 0.1) -> None:
        self.calls_by_key: dict[CausePayloadKey, int] = {}
        self.delay_s = delay_s
        self._lock = threading.Lock()

    def load(self, key: CausePayloadKey) -> CausePayload:
        with self._lock:
            self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
        time.sleep(self.delay_s)
        return CausePayload(key=key, payload={"brand": key.brand}, loaded_at=time.monotonic())


def test_metrics_cache_single_flight_shares_one_load() -> None:
    reader = CountingSnapshotReader()
    cache = TtlMetricsCache(reader, ttl_seconds=300)
    results: list[CacheSnapshot] = []

    def hit() -> None:
        results.append(cache.snapshot())

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert reader.calls == 1
    assert len(results) == 4
    assert all(snapshot is results[0] for snapshot in results)


def test_cause_payload_cache_single_flight_same_key() -> None:
    reader = CountingCauseReader()
    cache = TtlCausePayloadCache(reader, ttl_seconds=300, max_keys=8)
    key = _cause_key("리바로")
    results: list[CausePayload] = []

    def hit() -> None:
        results.append(cache.payload(key))

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert reader.calls_by_key[key] == 1
    assert all(payload is results[0] for payload in results)


def test_cause_payload_cache_evicts_lru_and_reloads() -> None:
    reader = CountingCauseReader(delay_s=0.0)
    cache = TtlCausePayloadCache(reader, ttl_seconds=300, max_keys=2)
    key_a, key_b, key_c = _cause_key("리바로"), _cause_key("리바로젯"), _cause_key("페린젝트")

    cache.payload(key_a)
    cache.payload(key_b)
    cache.payload(key_c)  # key_a는 LRU로 제거

    assert reader.calls_by_key[key_a] == 1
    cache.payload(key_b)  # 캐시 히트: 재로드 없음
    assert reader.calls_by_key[key_b] == 1
    cache.payload(key_a)  # 제거된 키 재조회 → 재로드
    assert reader.calls_by_key[key_a] == 2


def test_shared_caches_are_process_singletons() -> None:
    assert shared_metrics_cache(300) is shared_metrics_cache(300)
    assert shared_cause_payload_cache(300) is shared_cause_payload_cache(300)
    assert shared_metrics_cache(300) is not shared_metrics_cache(301)


def test_session_store_caps_sessions_with_lru() -> None:
    store = SessionStore(max_sessions=3)
    ids = [store.put({"n": index}) for index in range(4)]

    assert store.get(ids[0]) is None  # 가장 오래된 세션 제거
    assert store.get(ids[1]) == {"n": 1}
    assert store.get(ids[2]) == {"n": 2}
    assert store.get(ids[3]) == {"n": 3}

    store.get(ids[1])  # 최근 사용으로 갱신
    ids.append(store.put({"n": 4}))
    assert store.get(ids[1]) == {"n": 1}
    assert store.get(ids[2]) is None  # LRU였던 세션이 제거


def test_limiter_slot_times_out_and_recovers() -> None:
    limiter = ChatConcurrencyLimiter(max_concurrency=1, queue_wait_s=0.1)
    assert limiter.try_acquire()
    try:
        started = time.monotonic()
        try:
            with limiter.slot():
                raise AssertionError("slot must not be granted while occupied")
        except ChatBusyError:
            pass
        assert time.monotonic() - started >= 0.1
    finally:
        limiter.release()
    with limiter.slot():
        pass


class _CountingBlockingFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, *, external_mode: str = "live"):
        self.calls += 1
        factory = self

        class _Agent:
            def answer(self, question: str, _documents=None) -> dict:
                factory.started.set()
                factory.release.wait(timeout=10)
                return {"answer": f"ok:{question}", "sources": ["cache"], "tool_calls": []}

        return _Agent()


def test_chat_rejects_excess_request_before_llm_call() -> None:
    factory = _CountingBlockingFactory()
    limiter = ChatConcurrencyLimiter(max_concurrency=1, queue_wait_s=0.2)
    client = TestClient(create_app(agent_factory=factory, concurrency_limiter=limiter))

    responses: list[int] = []

    def occupy() -> None:
        responses.append(client.post("/chat", json={"question": "리바로 최근 실적 알려줘"}).status_code)

    occupant = threading.Thread(target=occupy)
    occupant.start()
    assert factory.started.wait(timeout=10)

    rejected = client.post("/chat", json={"question": "리바로 최근 실적 알려줘"})
    assert rejected.status_code == 503
    assert BUSY_MESSAGE in rejected.text
    assert factory.calls == 1  # 거절 요청은 LLM(agent) 경로에 도달하지 않음

    health = client.get("/healthz")
    assert health.status_code == 200  # 헬스체크는 세마포어 밖

    factory.release.set()
    occupant.join(timeout=10)
    assert responses == [200]

    follow_up = client.post("/chat", json={"question": "리바로 최근 실적 알려줘"})
    assert follow_up.status_code == 200  # 슬롯 반환 후 정상 처리
    assert factory.calls == 2  # 점유 1회 + 후속 1회, 거절 요청은 0회


def test_chat_stream_emits_busy_notice_without_llm_call() -> None:
    factory = _CountingBlockingFactory()
    limiter = ChatConcurrencyLimiter(max_concurrency=1, queue_wait_s=0.1)
    client = TestClient(create_app(agent_factory=factory, concurrency_limiter=limiter))

    assert limiter.try_acquire()  # 슬롯 선점
    try:
        response = client.get("/chat/stream", params={"question": "리바로 최근 실적 알려줘"})
        assert response.status_code == 200
        body = response.text
        assert BUSY_MESSAGE in body
        assert "event: done" in body
        assert body.rstrip().endswith("data: error")
        assert factory.calls == 0  # LLM 무호출
    finally:
        limiter.release()


def test_single_request_path_is_unchanged() -> None:
    factory = _CountingBlockingFactory()
    factory.release.set()
    client = TestClient(create_app(agent_factory=factory))

    response = client.post("/chat", json={"question": "리바로 최근 실적 알려줘"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"]
    assert payload["sources"] == ["cache"]
