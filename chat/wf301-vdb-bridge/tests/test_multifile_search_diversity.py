from __future__ import annotations

from src import main, weaviate_ops


def _hit(document_id: int, chunk_id: str, text: str = "evidence") -> dict[str, object]:
    return {
        "doc_id": document_id,
        "file_name": f"file-{document_id}.txt",
        "text": text,
        "summary": "{}",
        "i_page": 1,
        "i_chunk_on_doc": 1,
        "_additional": {"id": chunk_id, "distance": 0.1},
    }


def test_vector_search_supplements_documents_missing_from_global_top_k(monkeypatch) -> None:
    calls: list[tuple[list[int], int]] = []

    monkeypatch.setattr(weaviate_ops, "embed_text", lambda *args, **kwargs: [0.1])

    def fake_search(client, *, vector, doc_ids, limit):
        calls.append((doc_ids, limit))
        if doc_ids == [11, 22]:
            return [_hit(11, "a1"), _hit(11, "a2"), _hit(11, "a3")]
        if doc_ids == [22]:
            return [_hit(22, "b1")]
        raise AssertionError(f"unexpected document scope: {doc_ids}")

    monkeypatch.setattr(weaviate_ops, "search_target_chunks", fake_search)

    hits = main._search_document_hits(object(), "모든 파일을 요약해줘", [11, 22], 3)

    assert [hit["doc_id"] for hit in hits] == [11, 22, 11]
    assert calls == [([11, 22], 3), ([22], 1)]


def test_context_budget_keeps_evidence_from_each_retrieved_document() -> None:
    context, sources, empty_pages = main._context_from_hits(
        [_hit(11, "a1", "A" * 100), _hit(22, "b1", "B" * 100)],
        char_limit=20,
    )

    assert [source.document_id for source in sources] == [11, 22]
    assert "file-11.txt" in context
    assert "file-22.txt" in context
    assert "A" in context and "B" in context
    assert empty_pages == []


def test_explicit_file_name_limits_retrieval_to_that_session_document() -> None:
    rows = [
        {"document_id": 11, "file_name": "atu_di.xlsx"},
        {"document_id": 22, "file_name": "pdrn_bpi.xlsx"},
    ]

    selected = main._rows_requested_by_file_name("pdrn_bpi.xlsx만 요약해줘", rows)

    assert [row["document_id"] for row in selected] == [22]
