from __future__ import annotations

from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from pipeline.etl.io.iqvia_cache_storage import MinioCacheStorage
from pipeline.etl.io.iqvia_parquet_cache import (
    MinioCacheStorage as PublicMinioCacheStorage,
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
