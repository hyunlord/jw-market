from __future__ import annotations

import logging
from typing import Any

from src import session_wiki, settings, weaviate_ops


def _documents(*chunk_counts: int) -> list[dict[str, Any]]:
    return [
        {
            "document_id": index,
            "file_name": f"document-{index}.txt",
            "chunk_count": chunk_count,
        }
        for index, chunk_count in enumerate(chunk_counts, start=1)
    ]


def test_quota_keeps_small_documents_in_218_1_1_session() -> None:
    allocations = session_wiki._allocate_chunk_quotas(_documents(218, 1, 1), 80)

    assert allocations == [(1, 78), (2, 1), (3, 1)]
    assert sum(quota for _document_id, quota in allocations) == 80


def test_quota_redistributes_remainder_proportionally_and_deterministically() -> None:
    allocations = session_wiki._allocate_chunk_quotas(_documents(30, 30, 30), 80)

    assert allocations == [(1, 27), (2, 27), (3, 26)]


def test_single_document_keeps_existing_limit_behavior() -> None:
    allocations = session_wiki._allocate_chunk_quotas(_documents(218), 80)

    assert allocations == [(1, 80)]


def test_more_documents_than_limit_selects_first_ids_and_logs_exclusions(caplog) -> None:
    documents = _documents(*([1] * 82))

    with caplog.at_level(logging.WARNING, logger=session_wiki.__name__):
        allocations = session_wiki._allocate_chunk_quotas(documents, 80)

    assert allocations == [(document_id, 1) for document_id in range(1, 81)]
    assert "excluded_document_ids=[81, 82]" in caplog.text


def test_loader_queries_each_document_with_quota_and_returns_stable_chunk_order(monkeypatch) -> None:
    documents = _documents(30, 30, 30)
    calls: list[tuple[tuple[int, ...], int]] = []

    class _Client:
        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def read_chunks(_client: Any, doc_ids: list[int], limit: int) -> list[dict[str, Any]]:
        calls.append((tuple(doc_ids), limit))
        document_id = doc_ids[0]
        return [
            {
                "chunk_id": f"{document_id}-{index}",
                "doc_id": document_id,
                "i_page": 1,
                "i_chunk_on_doc": index,
                "text": str(index),
            }
            for index in reversed(range(limit))
        ]

    monkeypatch.setattr(session_wiki.httpx, "Client", _Client)
    monkeypatch.setattr(weaviate_ops, "read_target_chunks", read_chunks)
    monkeypatch.setattr(settings, "WIKI_MAX_CHUNKS", 80)

    first = session_wiki._load_chunks(documents)
    second = session_wiki._load_chunks(list(reversed(documents)))

    expected_calls = [((1,), 27), ((2,), 27), ((3,), 26)]
    assert calls == [*expected_calls, *expected_calls]
    assert first == second
    assert len(first) == 80
    assert [(row["doc_id"], row["i_chunk_on_doc"]) for row in first[:3]] == [
        (1, 0),
        (1, 1),
        (1, 2),
    ]
