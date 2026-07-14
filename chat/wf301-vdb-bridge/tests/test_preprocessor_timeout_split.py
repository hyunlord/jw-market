from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from src import settings, upload_adapter


class _ConfigCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __enter__(self) -> "_ConfigCursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, *_args: Any) -> None:
        return None

    def fetchone(self) -> dict[str, Any]:
        return self._rows.pop(0)


class _ConfigConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def cursor(self) -> _ConfigCursor:
        return _ConfigCursor(self._rows)


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "temp_vdb_index_id": 1,
            "temp_vdb_index": "control-index",
        }


@dataclass
class _RecordingClient:
    timeouts: list[float] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def post(self, _url: str, **kwargs: Any) -> _Response:
        self.timeouts.append(float(kwargs["timeout"]))
        self.bodies.append(kwargs["json"])
        return _Response()


def _config() -> upload_adapter.FileUploadConfig:
    return upload_adapter.FileUploadConfig(
        serving_id=25,
        preprocessor_id=64,
        batch_size=64,
        preprocessor_params={},
        lifespan_days=7,
        allowed_extensions=frozenset({"pdf"}),
    )


def test_run_preprocessor_uses_dedicated_timeout(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HTTP_TIMEOUT_S", 15.0)
    monkeypatch.setattr(settings, "PREPROCESSOR_TIMEOUT_S", 45.0, raising=False)
    client = _RecordingClient()

    upload_adapter.run_preprocessor(
        client,
        temp_vdb_index="control-index",
        config=_config(),
        saved_documents=[],
        user_id=None,
    )

    assert client.timeouts == [45.0]


def test_run_preprocessor_keeps_workflow_embedding_batch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VDB_INDEX_BATCH_SIZE", 256, raising=False)
    client = _RecordingClient()

    upload_adapter.run_preprocessor(
        client,
        temp_vdb_index="control-index",
        config=_config(),
        saved_documents=[],
        user_id=None,
    )

    assert client.bodies[0]["batch_size"] == 64


def test_load_file_upload_config_clamps_stale_large_embedding_batch() -> None:
    conn = _ConfigConnection(
        [
            {"values": {"preprocessor": 64, "batchSize": 256}},
            {"exts": "pdf,pptx"},
        ]
    )

    config = upload_adapter.load_file_upload_config(conn, workflow_id=301)

    assert config.batch_size == 64


class _TimeoutClient:
    def __init__(self, error_type: type[httpx.TimeoutException]) -> None:
        self.error_type = error_type

    def post(self, url: str, **_kwargs: Any) -> _Response:
        request = httpx.Request("POST", url)
        raise self.error_type("internal timeout details", request=request)


@pytest.mark.parametrize(
    "error_type",
    [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
)
def test_run_preprocessor_converts_http_timeout_to_safe_failure(
    error_type: type[httpx.TimeoutException],
) -> None:
    document = upload_adapter.SavedTempDocument(
        temp_document_id=1,
        file_name="large-report.pdf",
        file_path="/private/path/large-report.pdf",
    )

    with pytest.raises(upload_adapter.PreprocessorRunError) as caught:
        upload_adapter.run_preprocessor(
            _TimeoutClient(error_type),
            temp_vdb_index="control-index",
            config=_config(),
            saved_documents=[document],
            user_id=None,
        )

    assert caught.value.file_names == ["large-report.pdf"]
    assert caught.value.reason == (
        "문서가 커서 처리 시간이 초과되었습니다. 파일을 나누거나 페이지 범위를 줄여 다시 시도해 주세요."
    )
    assert "ReadTimeout" not in caught.value.reason
    assert "/private/path" not in caught.value.reason


def test_failure_envelope_keeps_internal_reason_out_of_user_message() -> None:
    class _FailureResponse(_Response):
        def json(self) -> dict[str, Any]:
            return {"code": 1, "errMsg": "worker failed at /private/path/document.pdf"}

    class _FailureClient:
        def post(self, _url: str, **_kwargs: Any) -> _FailureResponse:
            return _FailureResponse()

    with pytest.raises(upload_adapter.PreprocessorRunError) as caught:
        upload_adapter.run_preprocessor(
            _FailureClient(),
            temp_vdb_index="control-index",
            config=_config(),
            saved_documents=[],
            user_id=None,
        )

    assert "/private/path" in caught.value.reason
    assert caught.value.user_message == (
        "문서 전처리에 실패했습니다. 파일 형식이나 내용을 확인한 뒤 다시 시도해 주세요."
    )
    assert "/private/path" not in caught.value.user_message


def test_temp_vdb_call_keeps_global_http_timeout(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HTTP_TIMEOUT_S", 15.0)
    monkeypatch.setattr(settings, "PREPROCESSOR_TIMEOUT_S", 45.0, raising=False)
    client = _RecordingClient()

    upload_adapter.create_temp_vdb_index(
        client,
        app_session_id="control-session",
        lifespan_days=7,
        user_id=None,
    )

    assert client.timeouts == [15.0]
