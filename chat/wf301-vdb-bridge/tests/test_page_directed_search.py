from __future__ import annotations

from src import main, weaviate_ops


def test_requested_page_number_is_parsed_without_confusing_years() -> None:
    assert main._requested_page_number("31페이지 KOL 인용을 알려줘") == 31
    assert main._requested_page_number("page 10 표를 요약해줘") == 10
    assert main._requested_page_number("27번 슬라이드의 브랜드 표를 알려줘") == 27
    assert main._requested_page_number("슬라이드 12에 뭐 있어") == 12
    assert main._requested_page_number("2023년부터 2043년까지 환자수") is None


def test_page_directed_search_reads_exact_page_without_embedding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_page_chunks(client, *, doc_ids, page_number, limit):
        captured.update(doc_ids=doc_ids, page_number=page_number, limit=limit)
        return [
            {
                "text": "KOL: clinical adoption depends on durable outcomes.",
                "summary": "{}",
                "doc_id": 7,
                "file_name": "report.pdf",
                "i_page": 31,
                "i_chunk_on_doc": 88,
                "_additional": {"id": "chunk-31"},
            }
        ]

    monkeypatch.setattr(weaviate_ops, "read_target_page_chunks", fake_page_chunks)
    monkeypatch.setattr(weaviate_ops, "search_target_keyword_chunks", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        weaviate_ops,
        "embed_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("embedding must not run")),
    )

    hits = main._search_document_hits(object(), "31페이지 KOL 인용", [7], 8)

    assert captured == {"doc_ids": [7], "page_number": 31, "limit": 100}
    assert hits[0]["i_page"] == 31
    assert "durable outcomes" in hits[0]["text"]


def test_keyword_evidence_search_precedes_vector_results(monkeypatch) -> None:
    keyword = {"text": "KOL evidence", "_additional": {"id": "keyword"}}
    vector = {"text": "overview", "_additional": {"id": "vector"}}
    monkeypatch.setattr(weaviate_ops, "search_target_keyword_chunks", lambda *args, **kwargs: [keyword])
    monkeypatch.setattr(weaviate_ops, "embed_text", lambda *args, **kwargs: [0.1])
    monkeypatch.setattr(weaviate_ops, "search_target_chunks", lambda *args, **kwargs: [vector])

    hits = main._search_document_hits(object(), "KOL 발언이 문서에 있나?", [7], 8)

    assert [hit["_additional"]["id"] for hit in hits] == ["keyword", "vector"]
