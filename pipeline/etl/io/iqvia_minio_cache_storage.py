"""S3-compatible MinIO implementation of the IQVIA cache storage boundary."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib import error, parse, request
from xml.etree import ElementTree


class _Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _Response(Protocol):
    headers: _Headers

    def __enter__(self) -> "_Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


class _Opener(Protocol):
    def __call__(
        self,
        request_value: request.Request,
        *,
        timeout: float,
    ) -> _Response: ...


class MinioCacheStorageError(RuntimeError):
    """Raised when the MinIO object store rejects a cache operation."""


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class MinioCacheStorage:
    """CacheStorage adapter backed by one confined MinIO bucket prefix."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        root_prefix: str,
        region: str = "us-east-1",
        opener: _Opener = request.urlopen,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket.strip("/")
        self._access_key = access_key
        self._secret_key = secret_key
        self._root_prefix = root_prefix.strip("/")
        self._region = region
        self._opener = opener
        if not self._endpoint or not self._bucket or not self._root_prefix:
            raise ValueError("endpoint, bucket, and root_prefix are required")

    def _physical_key(self, key: str) -> str:
        candidate = PurePosixPath(key)
        if not key or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"invalid cache object key: {key!r}")
        normalized = candidate.as_posix().strip("/")
        return f"{self._root_prefix}/{normalized}"

    def _request(
        self,
        method: str,
        *,
        key: str = "",
        query: dict[str, str] | None = None,
        content: bytes = b"",
    ) -> _Response:
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = parse.urlparse(self._endpoint).netloc
        path = f"/{self._bucket}" + (f"/{key}" if key else "")
        canonical_uri = parse.quote(path, safe="/")
        query_items = sorted((query or {}).items())
        canonical_query = parse.urlencode(query_items, quote_via=parse.quote)
        payload_hash = hashlib.sha256(content).hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(headers)
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in headers
        )
        canonical_request = "\n".join(
            (
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            )
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            )
        )
        signing_key = _sign(
            _sign(
                _sign(
                    _sign(f"AWS4{self._secret_key}".encode(), date_stamp),
                    self._region,
                ),
                "s3",
            ),
            "aws4_request",
        )
        signature = hmac.new(
            signing_key,
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self._endpoint}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        request_value = request.Request(
            url,
            data=content if method == "PUT" else None,
            method=method,
            headers={
                "Authorization": authorization,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
            },
        )
        try:
            return self._opener(request_value, timeout=60)
        except error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(f"s3://{self._bucket}/{key}") from exc
            raise MinioCacheStorageError(
                f"MinIO {method} failed with HTTP {exc.code}"
            ) from exc

    def exists(self, key: str) -> bool:
        try:
            with self._request("HEAD", key=self._physical_key(key)):
                return True
        except FileNotFoundError:
            return False

    def get_bytes(self, key: str) -> bytes:
        with self._request("GET", key=self._physical_key(key)) as response:
            return response.read()

    def put_bytes(self, key: str, content: bytes) -> None:
        with self._request(
            "PUT",
            key=self._physical_key(key),
            content=content,
        ):
            return None

    def put_file(self, key: str, source: Path) -> None:
        self.put_bytes(key, source.read_bytes())

    def get_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.get_bytes(key))

    def size(self, key: str) -> int:
        with self._request("HEAD", key=self._physical_key(key)) as response:
            value = response.headers.get("Content-Length")
        if value is None:
            raise MinioCacheStorageError("MinIO HEAD omitted Content-Length")
        return int(value)

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        physical_prefix = self._physical_key(prefix)
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {
                "list-type": "2",
                "prefix": physical_prefix,
                "max-keys": "1000",
            }
            if token is not None:
                query["continuation-token"] = token
            with self._request("GET", query=query) as response:
                root = ElementTree.fromstring(response.read())
            namespace = (
                root.tag.partition("}")[0] + "}"
                if root.tag.startswith("{")
                else ""
            )
            for element in root.iter(f"{namespace}Key"):
                if element.text is None:
                    continue
                relative = element.text.removeprefix(f"{self._root_prefix}/")
                keys.append(relative)
            truncated = root.findtext(f"{namespace}IsTruncated") == "true"
            token = root.findtext(f"{namespace}NextContinuationToken")
            if not truncated or token is None:
                return tuple(sorted(keys))
