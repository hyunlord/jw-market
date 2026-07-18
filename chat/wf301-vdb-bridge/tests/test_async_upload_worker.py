from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from src import main, pdf_progressive, upload_adapter
from src.models import BridgeRequest, CommitDocumentResult, CommitResponse
from src.upload_status import UploadFilePreview, UploadFileStatus


def _request(saved: upload_adapter.SavedTempDocument) -> BridgeRequest:
    return BridgeRequest(
        workflow_id=301,
        vdb_id=139,
        app_session_id="session-a",
        user_id=7,
        temp_documents=[
            {
                "temp_document_id": saved.temp_document_id,
                "file_name": saved.file_name,
                "file_path": saved.file_path,
            }
        ],
    )


def _config() -> upload_adapter.FileUploadConfig:
    return upload_adapter.FileUploadConfig(
        serving_id=1,
        preprocessor_id=91,
        batch_size=10,
        preprocessor_params={},
        lifespan_days=1,
        allowed_extensions=frozenset({"pdf"}),
    )


def test_persisted_file_card_reuses_matching_observation_without_rescanning(
    monkeypatch,
) -> None:
    temp_doc = _request(
        upload_adapter.SavedTempDocument(11, "report.pdf", "/tmp/report.pdf")
    ).temp_documents[0]
    observed = {
        11: {
            "file_name": "report.pdf",
            "file_type": "pdf",
            "size_bytes": 3,
            "title": "Report",
            "sheet_count": None,
            "sheets": [],
            "page_count": 1,
            "slide_count": None,
        }
    }
    monkeypatch.setattr(
        main,
        "_inspect_path_upload_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected rescan")),
    )

    payload = main._persisted_file_card(temp_doc, observed)

    assert payload == observed[11]
    assert payload is not observed[11]


def test_persisted_file_card_recomputes_when_observed_name_does_not_match(
    monkeypatch,
) -> None:
    temp_doc = _request(
        upload_adapter.SavedTempDocument(11, "report.pdf", "/tmp/report.pdf")
    ).temp_documents[0]
    observed = {11: {"file_name": "other.pdf"}}
    server_card = main.StatusUploadFileCard(
        file_name="report.pdf",
        file_type="pdf",
        size_bytes=3,
        title="Report",
        page_count=1,
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main,
        "_inspect_path_upload_card",
        lambda file_path, file_name: calls.append((file_path, file_name)) or server_card,
    )

    payload = main._persisted_file_card(temp_doc, observed)

    assert calls == [("/tmp/report.pdf", "report.pdf")]
    assert payload is not None
    assert payload["file_name"] == "report.pdf"


def test_accepted_upload_worker_transitions_to_ready(monkeypatch, tmp_path: Path) -> None:
    saved = upload_adapter.SavedTempDocument(11, "report.pdf", str(tmp_path / "report.pdf"))
    Path(saved.file_path).write_bytes(b"pdf")
    transitions: list[tuple[str, tuple[UploadFileStatus, ...] | None, str | None]] = []
    observed_file_cards = {
        11: {
            "file_name": "report.pdf",
            "file_type": "pdf",
            "size_bytes": 3,
            "title": "Report",
            "sheet_count": None,
            "sheets": [],
            "page_count": 1,
            "slide_count": None,
        }
    }
    committed_cards: list[dict[int, dict[str, object]] | None] = []

    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "transition",
        lambda **kwargs: transitions.append(
            (kwargs["state"], tuple(kwargs["files"]) if kwargs.get("files") else None, kwargs.get("message"))
        ),
    )
    monkeypatch.setattr(upload_adapter, "run_preprocessor", lambda *args, **kwargs: {})
    def commit_documents(request, *, observed_file_cards=None):
        committed_cards.append(observed_file_cards)
        return CommitResponse(
            commit_enabled=True,
            write_count=1,
            target_vdb_id=139,
            target_collection="File139",
            workflow_id=301,
            app_session_id="session-a",
            documents=[
                CommitDocumentResult(
                    temp_document_id=11,
                    file_name="report.pdf",
                    source_doc_key="temp:11:report.pdf",
                    source_collection="Temp",
                    chunk_count=4,
                    route="vdb",
                    vector_dim=768,
                    status="committed",
                )
            ],
            committed_count=1,
            file_only_ready=True,
        )

    monkeypatch.setattr(main, "_commit_temp_documents", commit_documents)

    main._process_accepted_upload(
        upload_id="upl_7Qz4R4R2Xh9pCkN8",
        session_id="session-a",
        config=_config(),
        temp_vdb=upload_adapter.TempVdbIndex(3, "Temp"),
        saved_documents=[saved],
        request=_request(saved),
        observed_file_cards=observed_file_cards,
    )

    assert [state for state, _, _ in transitions] == ["preprocessing", "committing", "ready"]
    ready_files = transitions[-1][1]
    assert ready_files == (
        UploadFileStatus(
            file_name="report.pdf",
            state="ready",
            route="vdb",
            message="파일 처리가 완료되었습니다.",
        ),
    )
    assert committed_cards == [observed_file_cards]


def test_accepted_upload_worker_fails_closed_with_safe_status(monkeypatch, tmp_path: Path) -> None:
    saved = upload_adapter.SavedTempDocument(11, "report.pdf", str(tmp_path / "secret-report.pdf"))
    Path(saved.file_path).write_bytes(b"pdf")
    transitions: list[dict[str, object]] = []
    deactivated: list[int] = []

    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "transition",
        lambda **kwargs: transitions.append(kwargs),
    )

    def fail_preprocessor(*args, **kwargs):
        raise upload_adapter.PreprocessorRunError(
            "httpx timeout at /private/secret-report.pdf",
            file_names=["report.pdf"],
            user_message=upload_adapter.PREPROCESSOR_TIMEOUT_MESSAGE,
        )

    monkeypatch.setattr(upload_adapter, "run_preprocessor", fail_preprocessor)
    monkeypatch.setattr(upload_adapter, "cleanup_saved_documents", lambda documents: None)

    @contextmanager
    def connection():
        class Connection:
            def commit(self) -> None:
                pass

        yield Connection()

    monkeypatch.setattr(main.ledger, "ledger_connection", connection)
    monkeypatch.setattr(
        upload_adapter,
        "deactivate_temp_documents",
        lambda conn, *, temp_document_ids: deactivated.extend(temp_document_ids),
    )

    main._process_accepted_upload(
        upload_id="upl_7Qz4R4R2Xh9pCkN8",
        session_id="session-a",
        config=_config(),
        temp_vdb=upload_adapter.TempVdbIndex(3, "Temp"),
        saved_documents=[saved],
        request=_request(saved),
    )

    assert [item["state"] for item in transitions] == ["preprocessing", "blocked"]
    assert deactivated == [11]
    terminal = transitions[-1]
    assert terminal["message"] == upload_adapter.PREPROCESSOR_TIMEOUT_MESSAGE
    serialized = repr(terminal)
    assert "/private/" not in serialized
    assert "httpx" not in serialized


def test_long_pdf_becomes_queryable_from_preview_before_full_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = upload_adapter.SavedTempDocument(11, "report.pdf", str(tmp_path / "report.pdf"))
    preview_saved = upload_adapter.SavedTempDocument(
        1_812_345_678,
        "report.pdf",
        str(tmp_path / ".preview_TEMP_DOCUMENT_1812345678_report.pdf"),
    )
    Path(source.file_path).write_bytes(b"full")
    Path(preview_saved.file_path).write_bytes(b"preview")
    preview = pdf_progressive.PdfPreviewArtifact(
        preview_saved,
        indexed_pages=20,
        total_pages=185,
        source_temp_document_id=source.temp_document_id,
    )
    transitions: list[dict[str, object]] = []
    preprocess_calls: list[list[int]] = []
    cleaned: list[int] = []

    monkeypatch.setattr(main.settings, "PDF_PROGRESSIVE_PREVIEW_PAGES", 20)
    monkeypatch.setattr(main.pdf_progressive, "build_pdf_preview", lambda *args, **kwargs: preview)
    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "transition",
        lambda **kwargs: transitions.append(kwargs),
    )
    monkeypatch.setattr(
        upload_adapter,
        "run_preprocessor",
        lambda client, *, saved_documents, **kwargs: preprocess_calls.append(
            [item.temp_document_id for item in saved_documents]
        ),
    )
    chunk_counts = iter((0, 4))
    monkeypatch.setattr(
        main.weaviate_ops,
        "count_temp_chunks",
        lambda *args, **kwargs: next(chunk_counts),
    )
    monkeypatch.setattr(
        main.weaviate_ops,
        "delete_temp_objects_for_document",
        lambda client, *, temp_document_id, **kwargs: cleaned.append(temp_document_id) or ["a"],
    )
    monkeypatch.setattr(
        main,
        "_commit_temp_documents",
        lambda request: CommitResponse(
            commit_enabled=True,
            write_count=1,
            target_vdb_id=139,
            target_collection="File139",
            workflow_id=301,
            app_session_id="session-a",
            documents=[
                CommitDocumentResult(
                    temp_document_id=11,
                    file_name="report.pdf",
                    source_doc_key="temp:11:report.pdf",
                    source_collection="Temp",
                    chunk_count=40,
                    route="vdb",
                    vector_dim=768,
                    status="committed",
                )
            ],
            committed_count=1,
            file_only_ready=True,
        ),
    )

    main._process_accepted_upload(
        upload_id="upl_7Qz4R4R2Xh9pCkN8",
        session_id="session-a",
        config=_config(),
        temp_vdb=upload_adapter.TempVdbIndex(3, "TempPreview"),
        saved_documents=[source],
        request=_request(source),
    )

    assert preprocess_calls == [[1_812_345_678], [11]]
    assert [item["state"] for item in transitions] == [
        "preprocessing",
        "preprocessing",
        "committing",
        "ready",
    ]
    preview_files = transitions[1]["files"]
    assert preview_files == (
        UploadFileStatus(
            "report.pdf",
            state="preprocessing",
            message="앞 20/185페이지는 지금 질문할 수 있습니다.",
            preview=UploadFilePreview(
                temp_document_id=1_812_345_678,
                collection="TempPreview",
                indexed_pages=20,
                total_pages=185,
                file_name="report.pdf",
            ),
        ),
    )
    committing_files = transitions[2]["files"]
    assert committing_files[0].state == "committing"
    assert committing_files[0].preview == preview_files[0].preview
    assert "질문할 수 있습니다" in (committing_files[0].message or "")
    assert cleaned == [1_812_345_678]
    assert not Path(preview_saved.file_path).exists()


def test_preview_status_matches_duplicate_file_names_by_source_document_id() -> None:
    first = upload_adapter.SavedTempDocument(11, "report.pdf", "/tmp/first.pdf")
    second = upload_adapter.SavedTempDocument(12, "report.pdf", "/tmp/second.pdf")
    previews = [
        pdf_progressive.PdfPreviewArtifact(
            upload_adapter.SavedTempDocument(1_800_000_011, "report.pdf", "/tmp/p1.pdf"),
            indexed_pages=10,
            total_pages=50,
            source_temp_document_id=11,
        )
    ]

    statuses = main._preview_file_statuses([first, second], previews, "TempPreview")

    assert statuses[0].preview is not None
    assert statuses[0].preview.temp_document_id == 1_800_000_011
    assert statuses[1].preview is None
