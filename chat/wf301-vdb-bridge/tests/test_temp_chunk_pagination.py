from __future__ import annotations

import re
from typing import Any

import pytest

from src import weaviate_ops


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, chunks: list[dict[str, Any]], *, aggregate_count: int | None = None) -> None:
        self._chunks = chunks
        self._aggregate_count = len(chunks) if aggregate_count is None else aggregate_count
        self.queries: list[str] = []

    def post(self, _url: str, *, json: dict[str, str], timeout: float) -> _Response:
        del timeout
        query = json["query"]
        self.queries.append(query)
        if "Aggregate" in query:
            return _Response(
                {"data": {"Aggregate": {"TempChunk": [{"meta": {"count": self._aggregate_count}}]}}}
            )

        limit_match = re.search(r"limit:(\d+)", query)
        assert limit_match is not None
        limit = int(limit_match.group(1))
        after_match = re.search(r'after:"([^"]+)"', query)
        start = 0
        if after_match is not None:
            ids = [str(row["_additional"]["id"]) for row in self._chunks]
            start = ids.index(after_match.group(1)) + 1
        rows = self._chunks[start : start + limit]
        return _Response({"data": {"Get": {"TempChunk": rows}}})


def _chunks(count: int) -> list[dict[str, Any]]:
    return [
        {
            "text": str(index),
            "i_page": index // 4 + 1,
            "i_chunk_on_doc": index,
            "i_chunk_on_page": index % 4,
            "_additional": {"id": f"00000000-0000-0000-0000-{index:012d}", "vector": [0.1]},
        }
        for index in range(count)
    ]


def test_read_temp_chunks_recovers_all_batches_over_weaviate_default_limit() -> None:
    client = _Client(_chunks(205))

    result = weaviate_ops.read_temp_chunks(client, "TempChunk", 77)

    assert len(result) == 205
    assert [row["i_chunk_on_doc"] for row in result] == list(range(205))
    get_queries = [query for query in client.queries if " Get " in query]
    assert len(get_queries) == 3
    assert "limit:100" in get_queries[0]
    assert 'after:"00000000-0000-0000-0000-000000000099"' in get_queries[1]
    assert 'after:"00000000-0000-0000-0000-000000000199"' in get_queries[2]


def test_read_temp_chunks_returns_stable_document_page_chunk_order() -> None:
    chunks = [_chunks(3)[2], _chunks(3)[0], _chunks(3)[1]]
    client = _Client(chunks)

    first = weaviate_ops.read_temp_chunks(client, "TempChunk", 77)
    second = weaviate_ops.read_temp_chunks(_Client(chunks), "TempChunk", 77)

    assert [row["i_chunk_on_doc"] for row in first] == [0, 1, 2]
    assert first == second


def test_read_temp_chunks_fails_closed_when_aggregate_count_exceeds_recovered_count() -> None:
    client = _Client(_chunks(200), aggregate_count=205)

    with pytest.raises(weaviate_ops.TempChunkCompletenessError, match="205 expected, 200 recovered"):
        weaviate_ops.read_temp_chunks(client, "TempChunk", 77)


def test_read_temp_chunks_keeps_short_documents_on_one_batch() -> None:
    client = _Client(_chunks(20))

    result = weaviate_ops.read_temp_chunks(client, "TempChunk", 77)

    assert len(result) == 20
    assert sum(" Get " in query for query in client.queries) == 1


def test_read_temp_chunks_skips_get_when_aggregate_is_empty() -> None:
    client = _Client([])

    assert weaviate_ops.read_temp_chunks(client, "TempChunk", 77) == []
    assert len(client.queries) == 1
