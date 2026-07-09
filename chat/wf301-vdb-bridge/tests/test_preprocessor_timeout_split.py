from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src import settings, upload_adapter


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

    def post(self, _url: str, **kwargs: Any) -> _Response:
        self.timeouts.append(float(kwargs["timeout"]))
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
