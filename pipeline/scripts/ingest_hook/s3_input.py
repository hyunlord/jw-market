"""Minimal S3(SigV4) reader for MinIO submissions — stdlib only, read-only.

The submission set lives in the MinIO market bucket (site contract v2.1), not
on a mounted filesystem, so the trigger service and the ingest Job fetch the
manifest and data files over the S3 API. Deliberately dependency-free (the
pinned orchestrator image carries no boto3/minio): GET + LIST with AWS SigV4,
path-style addressing.

Write operations are intentionally absent — the hook must never mutate the
submission bucket.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

ENV_BUCKET = "INGEST_S3_BUCKET"


class S3InputError(RuntimeError):
    pass


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


@dataclass
class S3Input:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    # test seam: (urllib.request.Request) -> bytes
    opener=None

    @classmethod
    def from_env(cls) -> "S3Input | None":
        bucket = os.environ.get(ENV_BUCKET, "").strip()
        if not bucket:
            return None
        endpoint = os.environ.get("MINIO_ENDPOINT", "").strip()
        access = os.environ.get("MINIO_ACCESS_KEY", "")
        secret = os.environ.get("MINIO_SECRET_KEY", "")
        if not (endpoint and access and secret):
            raise S3InputError(
                f"{ENV_BUCKET} is set but MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY are incomplete"
            )
        return cls(
            endpoint=endpoint.rstrip("/"),
            bucket=bucket,
            access_key=access,
            secret_key=secret,
            region=os.environ.get("MINIO_REGION", "us-east-1"),
        )

    # -- SigV4 ----------------------------------------------------------------
    def _request(self, method: str, key: str = "", query: dict | None = None) -> bytes:
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        host = urllib.parse.urlparse(self.endpoint).netloc

        canonical_uri = "/" + urllib.parse.quote(f"{self.bucket}/{key}" if key else self.bucket)
        query = dict(sorted((query or {}).items()))
        canonical_query = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        payload_hash = hashlib.sha256(b"").hexdigest()
        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        signed_headers = ";".join(headers)
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in headers)
        canonical_request = "\n".join(
            (method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash)
        )
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            ("AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
        )
        signing_key = _sign(
            _sign(_sign(_sign(f"AWS4{self.secret_key}".encode(), datestamp), self.region), "s3"),
            "aws4_request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self.endpoint}{canonical_uri}" + (f"?{canonical_query}" if canonical_query else "")
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": authorization,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
            },
        )
        if self.opener is not None:
            return self.opener(request)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            if exc.code == 404:
                raise FileNotFoundError(f"s3://{self.bucket}/{key}") from exc
            raise S3InputError(f"S3 {method} {key}: HTTP {exc.code}") from exc

    # -- public surface ---------------------------------------------------------
    def read(self, key: str) -> bytes:
        key = key.lstrip("/")
        if ".." in key.split("/"):
            raise S3InputError(f"key escapes bucket namespace: {key!r}")
        return self._request("GET", key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            root = ElementTree.fromstring(self._request("GET", "", query))
            ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
            keys.extend(el.text for el in root.iter(f"{ns}Key") if el.text)
            truncated = root.findtext(f"{ns}IsTruncated") == "true"
            token = root.findtext(f"{ns}NextContinuationToken")
            if not truncated or not token:
                return keys

    def materialize(self, keys: list[str], workdir: Path) -> Path:
        """Download keys under ``workdir`` preserving relative paths (for G3)."""
        for key in keys:
            target = workdir / key.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read(key))
        return workdir
