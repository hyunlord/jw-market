"""Configured ingest input backend selection and local confinement."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from ingest_fixtures import write_submission


def test_explicit_local_backend_ignores_legacy_s3_configuration(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "local")
    monkeypatch.setenv("INGEST_INPUT_ROOT", "/nfs-root/autoIngestion")
    monkeypatch.setenv("INGEST_S3_BUCKET", "legacy-bucket")

    assert config.open_input_source() is None
    assert config.input_root() == Path("/nfs-root/autoIngestion")


def test_unknown_input_backend_fails_closed(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "filesystem-ish")

    with pytest.raises(RuntimeError, match="unsupported INGEST_INPUT_BACKEND"):
        config.open_input_source()


def test_explicit_s3_backend_preserves_existing_source(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "s3")
    monkeypatch.setenv("INGEST_S3_BUCKET", "jw-market-raw")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "read-only-user")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test-secret")

    source = config.open_input_source()

    assert source is not None
    assert source.bucket == "jw-market-raw"


def test_local_webhook_rejects_manifest_path_outside_root(
    tmp_path, sqlite_ledger, fake_transport
):
    root = tmp_path / "input"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    manifest = write_submission(outside)
    client = TestClient(
        create_app(IngestService(sqlite_ledger, root, transport=fake_transport))
    )

    response = client.post("/ingest/webhook", json={"manifest_path": str(manifest)})

    assert response.status_code == 400
    assert "escapes the input root" in response.json()["detail"]
