from __future__ import annotations

from typing import Any

import httpx

from src import weaviate_ops


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _EmbeddingClient:
    def __init__(
        self,
        *,
        partial_above: int | None = None,
        timeout_above: int | None = None,
    ) -> None:
        self.partial_above = partial_above
        self.timeout_above = timeout_above
        self.requests: list[list[str]] = []

    def post(
        self,
        _url: str,
        *,
        json: dict[str, Any],
        timeout: float,
    ) -> _Response:
        del timeout
        raw_input = json["input"]
        texts = [raw_input] if isinstance(raw_input, str) else list(raw_input)
        self.requests.append(texts)
        if self.timeout_above is not None and len(texts) > self.timeout_above:
            request = httpx.Request("POST", "http://embedding.test/v1/embeddings")
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        result_count = len(texts)
        if self.partial_above is not None and len(texts) > self.partial_above:
            result_count -= 1
        rows = [
            {"index": index, "embedding": [float(text.removeprefix("chunk-"))]}
            for index, text in enumerate(texts[:result_count])
        ]
        rows.reverse()
        return _Response({"data": rows})


def test_embedding_batch_size_never_exceeds_verified_serving_limit(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "512")
    monkeypatch.delenv("EMBEDDING_MAX_BATCH_SIZE", raising=False)

    assert weaviate_ops._embedding_batch_size() == 64


def test_embedding_batch_concurrency_defaults_to_one(monkeypatch) -> None:
    monkeypatch.delenv("EMBEDDING_BATCH_CONCURRENCY", raising=False)

    assert weaviate_ops._embedding_batch_concurrency() == 1


def test_embed_texts_batches_every_chunk_and_preserves_input_order(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "4")
    monkeypatch.setenv("EMBEDDING_BATCH_CONCURRENCY", "1")
    client = _EmbeddingClient()
    texts = [f"chunk-{index}" for index in range(10)]

    vectors = weaviate_ops.embed_texts(client, texts)

    assert [len(batch) for batch in client.requests] == [4, 4, 2]
    assert vectors == [[float(index)] for index in range(10)]


def test_embed_texts_splits_partial_batch_without_losing_chunks(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setenv("EMBEDDING_BATCH_CONCURRENCY", "1")
    client = _EmbeddingClient(partial_above=2)
    texts = [f"chunk-{index}" for index in range(5)]

    vectors = weaviate_ops.embed_texts(client, texts)

    assert [len(batch) for batch in client.requests] == [5, 2, 3, 1, 2]
    assert vectors == [[float(index)] for index in range(5)]


def test_embed_texts_splits_timeout_batch_without_losing_chunks(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setenv("EMBEDDING_FALLBACK_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_BATCH_CONCURRENCY", "1")
    client = _EmbeddingClient(timeout_above=2)
    texts = [f"chunk-{index}" for index in range(5)]

    vectors = weaviate_ops.embed_texts(client, texts)

    assert [len(batch) for batch in client.requests] == [5, 2, 2, 1]
    assert vectors == [[float(index)] for index in range(5)]
