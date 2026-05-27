from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "pipeline" / "scripts"))

from etl import storage  # noqa: E402


class FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        yield from self.pages


class FakeS3Client:
    def __init__(self, pages: list[dict] | None = None) -> None:
        self.paginator = FakePaginator(pages or [])
        self.downloads: list[tuple[str, str, str]] = []
        self.uploads: list[tuple[str, str, str]] = []

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return self.paginator

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.downloads.append((bucket, key, filename))
        Path(filename).write_text(f"{bucket}/{key}", encoding="utf-8")

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.uploads.append((filename, bucket, key))


def test_default_backend_is_local(monkeypatch):
    monkeypatch.delenv("ETL_STORAGE_BACKEND", raising=False)

    assert storage.get_storage_backend() == "local"
    assert storage.is_local_backend()
    assert not storage.is_minio_backend()


def test_minio_backend_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ETL_STORAGE_BACKEND", "MINIO")

    assert storage.get_storage_backend() == "minio"
    assert storage.is_minio_backend()
    assert not storage.is_local_backend()


def test_invalid_backend_raises(monkeypatch):
    monkeypatch.setenv("ETL_STORAGE_BACKEND", "s3")

    with pytest.raises(ValueError, match="ETL_STORAGE_BACKEND"):
        storage.get_storage_backend()


def test_default_work_dir(monkeypatch):
    monkeypatch.delenv("ETL_WORK_DIR", raising=False)

    assert storage.get_work_dir() == Path("/tmp/jw-market-etl")


def test_custom_work_dir(monkeypatch):
    monkeypatch.setenv("ETL_WORK_DIR", "/mnt/etl")

    assert storage.get_work_dir() == Path("/mnt/etl")


def test_get_data_path_returns_local_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ETL_STORAGE_BACKEND", "local")
    local_default = tmp_path / "data" / "UBIST"

    result = storage.get_data_path(
        "MINIO_BUCKET_RAW_UBIST",
        "jw-market-raw-ubist",
        local_default,
    )

    assert result == local_default


def test_create_minio_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="MinIO credentials missing"):
        storage.create_minio_client()


def test_create_minio_client_accepts_credentials(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    assert boto3 is not None
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
    monkeypatch.setenv("MINIO_SECURE", "false")

    client = storage.create_minio_client()

    assert client.meta.endpoint_url == "http://localhost:9000"


def test_sync_minio_to_local_downloads_missing_files(tmp_path):
    client = FakeS3Client(
        [
            {
                "Contents": [
                    {"Key": "UBIST/a.xlsx"},
                    {"Key": "UBIST/nested/b.xlsx"},
                ]
            }
        ]
    )

    count = storage.sync_minio_to_local(
        "jw-market-raw-ubist",
        "UBIST/",
        tmp_path,
        progress=False,
        client=client,
    )

    assert count == 2
    assert (tmp_path / "a.xlsx").exists()
    assert (tmp_path / "nested" / "b.xlsx").exists()


def test_sync_minio_to_local_skips_existing_without_overwrite(tmp_path):
    (tmp_path / "a.xlsx").write_text("old", encoding="utf-8")
    client = FakeS3Client([{"Contents": [{"Key": "UBIST/a.xlsx"}]}])

    count = storage.sync_minio_to_local(
        "jw-market-raw-ubist",
        "UBIST/",
        tmp_path,
        overwrite=False,
        progress=False,
        client=client,
    )

    assert count == 0
    assert client.downloads == []
    assert (tmp_path / "a.xlsx").read_text(encoding="utf-8") == "old"


def test_upload_local_to_minio_uploads_files(tmp_path):
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.txt").write_text("2", encoding="utf-8")
    client = FakeS3Client()

    count = storage.upload_local_to_minio(
        tmp_path,
        "jw-market-enriched",
        "enriched",
        progress=False,
        client=client,
    )

    assert count == 2
    uploaded_keys = {upload[2] for upload in client.uploads}
    assert uploaded_keys == {"enriched/one.txt", "enriched/nested/two.txt"}


def test_get_data_path_syncs_minio_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("ETL_STORAGE_BACKEND", "minio")
    monkeypatch.setenv("ETL_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MINIO_BUCKET_RAW_UBIST", "custom-ubist")
    client = FakeS3Client([{"Contents": [{"Key": "a.xlsx"}]}])

    result = storage.get_data_path(
        "MINIO_BUCKET_RAW_UBIST",
        "jw-market-raw-ubist",
        tmp_path / "local",
        work_subdir="raw-ubist",
        progress=False,
        client=client,
    )

    assert result == tmp_path / "work" / "raw-ubist"
    assert (result / "a.xlsx").exists()
    assert client.paginator.calls == [
        {"Bucket": "custom-ubist", "Prefix": ""},
    ]
