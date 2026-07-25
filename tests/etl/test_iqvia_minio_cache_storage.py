from __future__ import annotations

from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from pipeline.etl.io.iqvia_cache_storage import (
    MinioCacheStorage,
    build_iqvia_minio_cache_storage,
)
from pipeline.etl.io.iqvia_parquet_cache import (
    MinioCacheStorage as PublicMinioCacheStorage,
    build_iqvia_minio_cache_storage as public_build_iqvia_minio_cache_storage,
)


class _Response:
    def __init__(self, body: bytes = b"", *, content_length: int | None = None) -> None:
        self._body = body
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        assert timeout == 60
        self.requests.append(request)
        key = request.full_url.partition("/jw-market-raw-iqvia/")[2].partition("?")[0]
        if "list-type=2" in request.full_url:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "<IsTruncated>false</IsTruncated>"
                + "".join(f"<Contents><Key>{name}</Key></Contents>" for name in sorted(self.objects))
                + "</ListBucketResult>"
            )
            return _Response(xml.encode())
        match request.method:
            case "PUT":
                self.objects[key] = request.data or b""
                return _Response()
            case "GET":
                if key not in self.objects:
                    raise HTTPError(request.full_url, 404, "Not Found", {}, None)
                return _Response(self.objects[key])
            case "HEAD":
                if key not in self.objects:
                    raise HTTPError(request.full_url, 404, "Not Found", {}, None)
                return _Response(content_length=len(self.objects[key]))
            case unexpected:
                raise AssertionError(f"unexpected method: {unexpected}")


def _storage(fake: _FakeMinio) -> MinioCacheStorage:
    return MinioCacheStorage(
        endpoint="http://minio.example:9000",
        bucket="jw-market-raw-iqvia",
        access_key="access",
        secret_key="secret",
        root_prefix="derived/iqvia/nsa/parquet-cache/v1",
        opener=fake,
    )


def test_minio_adapter_round_trips_cache_storage_contract(tmp_path: Path) -> None:
    fake = _FakeMinio()
    storage = _storage(fake)
    source = tmp_path / "source.parquet"
    source.write_bytes(b"partition")

    storage.put_bytes("cache/manifest.json", b"manifest")
    storage.put_file("cache/part.parquet", source)
    destination = tmp_path / "downloaded.parquet"
    storage.get_file("cache/part.parquet", destination)

    assert storage.exists("cache/manifest.json")
    assert storage.get_bytes("cache/manifest.json") == b"manifest"
    assert storage.size("cache/part.parquet") == len(b"partition")
    assert destination.read_bytes() == b"partition"
    assert storage.list_keys("cache") == (
        "cache/manifest.json",
        "cache/part.parquet",
    )


def test_minio_adapter_returns_false_only_for_missing_object() -> None:
    fake = _FakeMinio()
    storage = _storage(fake)

    assert storage.exists("cache/missing") is False


def test_minio_adapter_rejects_keys_outside_cache_prefix() -> None:
    storage = _storage(_FakeMinio())

    with pytest.raises(ValueError, match="invalid cache object key"):
        storage.get_bytes("../raw/source.xlsx")


def test_minio_adapter_is_exposed_by_public_cache_module() -> None:
    assert PublicMinioCacheStorage is MinioCacheStorage


def test_build_iqvia_minio_cache_storage_is_exposed_by_public_cache_module() -> None:
    assert public_build_iqvia_minio_cache_storage is build_iqvia_minio_cache_storage


def _clear_iqvia_cache_minio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "IQVIA_CACHE_MINIO_ENDPOINT",
        "IQVIA_CACHE_MINIO_ACCESS_KEY",
        "IQVIA_CACHE_MINIO_SECRET_KEY",
        "IQVIA_CACHE_MINIO_BUCKET",
        "IQVIA_CACHE_MINIO_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)


def test_build_iqvia_minio_cache_storage_raises_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_iqvia_cache_minio_env(monkeypatch)

    with pytest.raises(RuntimeError, match="IQVIA cache MinIO credentials missing"):
        build_iqvia_minio_cache_storage()


def test_build_iqvia_minio_cache_storage_never_falls_back_to_generic_minio_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_iqvia_cache_minio_env(monkeypatch)
    # A generic MINIO_* credential (e.g. from the unrelated archival sync path)
    # must never be picked up by this dedicated, prefix-scoped cache adapter.
    monkeypatch.setenv("MINIO_ENDPOINT", "http://generic.example:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "generic-access")
    monkeypatch.setenv("MINIO_SECRET_KEY", "generic-secret")

    with pytest.raises(RuntimeError, match="IQVIA cache MinIO credentials missing"):
        build_iqvia_minio_cache_storage()


def test_build_iqvia_minio_cache_storage_uses_dedicated_env_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_iqvia_cache_minio_env(monkeypatch)
    monkeypatch.setenv("IQVIA_CACHE_MINIO_ENDPOINT", "http://minio.example:9000")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_ACCESS_KEY", "jw-iqvia-cache")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_SECRET_KEY", "scoped-secret")

    storage = build_iqvia_minio_cache_storage()

    assert isinstance(storage, MinioCacheStorage)
    assert storage._bucket == "jw-market-raw"
    assert storage._root_prefix == "iqvia-parquet-cache"
    assert storage._access_key == "jw-iqvia-cache"
    assert storage._secret_key == "scoped-secret"


def test_build_iqvia_minio_cache_storage_honors_bucket_and_prefix_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_iqvia_cache_minio_env(monkeypatch)
    monkeypatch.setenv("IQVIA_CACHE_MINIO_ENDPOINT", "http://minio.example:9000")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_ACCESS_KEY", "jw-iqvia-cache")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_SECRET_KEY", "scoped-secret")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_BUCKET", "custom-bucket")
    monkeypatch.setenv("IQVIA_CACHE_MINIO_PREFIX", "custom-prefix")

    storage = build_iqvia_minio_cache_storage()

    assert storage._bucket == "custom-bucket"
    assert storage._root_prefix == "custom-prefix"
