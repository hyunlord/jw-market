from __future__ import annotations

from contextlib import contextmanager

from src import main, weaviate_ops
from src.models import SearchRequest
from src.upload_status import UploadFilePreview


@contextmanager
def _connection():
    yield object()


def _request() -> SearchRequest:
    return SearchRequest(
        workflow_id=301,
        app_session_id="session-a",
        question="앞부분 핵심 결론이 뭐야?",
    )


def _install_empty_ledger(monkeypatch) -> None:
    monkeypatch.setattr(main.ledger, "ledger_connection", _connection)
    monkeypatch.setattr(main.ledger, "list_session_documents", lambda *args, **kwargs: [])


def test_search_uses_session_owned_pdf_preview_before_full_commit(monkeypatch) -> None:
    _install_empty_ledger(monkeypatch)
    preview = UploadFilePreview(
        file_name="report.pdf",
        temp_document_id=1_812_345_678,
        collection="TempPreview",
        indexed_pages=20,
        total_pages=185,
    )
    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "queryable_previews",
        lambda **kwargs: (preview,),
    )
    monkeypatch.setattr(weaviate_ops, "embed_text", lambda client, text: [0.1, 0.2])
    monkeypatch.setattr(
        weaviate_ops,
        "search_temp_chunks",
        lambda client, **kwargs: [
            {
                "text": "이 보고서는 치료 접근성 개선을 핵심 결론으로 제시합니다.",
                "temp_doc_id": 1_812_345_678,
                "file_name": "report.pdf",
                "i_page": 4,
                "i_chunk_on_doc": 8,
                "_additional": {"id": "preview-hit", "distance": 0.12},
            }
        ],
    )

    response = main.search(_request())

    assert response.document_count == 1
    assert response.result_count == 1
    assert "치료 접근성 개선" in response.file_context
    assert "인덱싱 진행 중 (앞 20/185페이지 기준)" in response.file_context
    assert "현재 검색 가능 범위만 반영" in response.file_context
    assert response.file_sources[0].file_name == "report.pdf"
    assert response.file_sources[0].i_page == 4


def test_empty_preview_search_never_claims_document_wide_absence(monkeypatch) -> None:
    _install_empty_ledger(monkeypatch)
    preview = UploadFilePreview(
        file_name="report.pdf",
        temp_document_id=1_812_345_678,
        collection="TempPreview",
        indexed_pages=20,
        total_pages=185,
    )
    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "queryable_previews",
        lambda **kwargs: (preview,),
    )
    monkeypatch.setattr(weaviate_ops, "embed_text", lambda client, text: [0.1, 0.2])
    monkeypatch.setattr(weaviate_ops, "search_temp_chunks", lambda *args, **kwargs: [])

    response = main.search(_request())

    assert response.document_count == 1
    assert response.result_count == 0
    assert "현재까지 인덱싱된 범위(앞 20/185페이지)에서는 확인되지 않았습니다" in response.file_context
    assert "전체 인덱싱은 진행 중" in response.file_context
    assert "문서에 없습니다" not in response.file_context


def test_preview_competes_by_relevance_with_already_committed_files() -> None:
    committed = {
        "text": "older session document",
        "_additional": {"id": "committed", "distance": 0.8},
    }
    preview = {
        "text": "current upload answer",
        "_additional": {"id": "preview", "distance": 0.1},
    }

    merged = main._merge_ranked_hits([committed], [preview], limit=1)

    assert merged == [preview]


def test_page_question_reads_exact_preview_page_without_embedding(monkeypatch) -> None:
    _install_empty_ledger(monkeypatch)
    preview = UploadFilePreview(
        file_name="report.pdf",
        temp_document_id=1_812_345_678,
        collection="TempPreview",
        indexed_pages=20,
        total_pages=185,
    )
    monkeypatch.setattr(main._UPLOAD_STATUS, "queryable_previews", lambda **kwargs: (preview,))
    monkeypatch.setattr(
        weaviate_ops,
        "embed_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("embedding must not run")),
    )
    monkeypatch.setattr(
        weaviate_ops,
        "read_temp_page_chunks",
        lambda client, **kwargs: [
            {
                "text": "3페이지의 정확한 근거",
                "temp_doc_id": 1_812_345_678,
                "file_name": "report.pdf",
                "i_page": 3,
                "i_chunk_on_doc": 5,
                "_additional": {"id": "page-3"},
            }
        ],
    )

    response = main.search(_request().model_copy(update={"question": "3페이지 내용 알려줘"}))

    assert response.result_count == 1
    assert "3페이지의 정확한 근거" in response.file_context


def test_page_outside_preview_scope_does_not_search_unrelated_pages(monkeypatch) -> None:
    _install_empty_ledger(monkeypatch)
    preview = UploadFilePreview(
        file_name="report.pdf",
        temp_document_id=1_812_345_678,
        collection="TempPreview",
        indexed_pages=20,
        total_pages=185,
    )
    monkeypatch.setattr(main._UPLOAD_STATUS, "queryable_previews", lambda **kwargs: (preview,))
    monkeypatch.setattr(
        weaviate_ops,
        "embed_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("embedding must not run")),
    )
    monkeypatch.setattr(
        weaviate_ops,
        "read_temp_page_chunks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("page is not indexed")),
    )

    response = main.search(_request().model_copy(update={"question": "100페이지 내용 알려줘"}))

    assert response.result_count == 0
    assert "현재까지 인덱싱된 범위(앞 20/185페이지)에서는 확인되지 않았습니다" in response.file_context
