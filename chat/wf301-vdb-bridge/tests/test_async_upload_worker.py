from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from src import main, upload_adapter
from src.models import BridgeRequest, CommitDocumentResult, CommitResponse
from src.upload_status import UploadFileStatus


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


def test_accepted_upload_worker_transitions_to_ready(monkeypatch, tmp_path: Path) -> None:
    saved = upload_adapter.SavedTempDocument(11, "report.pdf", str(tmp_path / "report.pdf"))
    Path(saved.file_path).write_bytes(b"pdf")
    transitions: list[tuple[str, tuple[UploadFileStatus, ...] | None, str | None]] = []

    monkeypatch.setattr(
        main._UPLOAD_STATUS,
        "transition",
        lambda **kwargs: transitions.append(
            (kwargs["state"], tuple(kwargs["files"]) if kwargs.get("files") else None, kwargs.get("message"))
        ),
    )
    monkeypatch.setattr(upload_adapter, "run_preprocessor", lambda *args, **kwargs: {})
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
                    chunk_count=4,
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
        temp_vdb=upload_adapter.TempVdbIndex(3, "Temp"),
        saved_documents=[saved],
        request=_request(saved),
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
