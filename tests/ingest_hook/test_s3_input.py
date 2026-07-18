"""S3(MinIO) submission source: reader, webhook mode, sweep listing."""
from __future__ import annotations

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.s3_input import S3Input, S3InputError
from pipeline.scripts.ingest_hook.sweep import sweep
from ingest_fixtures import write_submission


class FakeBucket:
    """Answers GET/LIST like MinIO; records the requests it saw."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.requests = []

    def __call__(self, request) -> bytes:
        self.requests.append(request)
        parsed = urllib.parse.urlparse(request.full_url)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
        if "list-type=2" in (parsed.query or ""):
            prefix = urllib.parse.parse_qs(parsed.query).get("prefix", [""])[0]
            keys = sorted(k for k in self.objects if k.startswith(prefix))
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            return (
                f"<ListBucketResult><IsTruncated>false</IsTruncated>{body}</ListBucketResult>"
            ).encode()
        key = parsed.path.split("/", 2)[2]  # /bucket/key
        key = urllib.parse.unquote(key)
        if key not in self.objects:
            raise FileNotFoundError(f"s3://bucket/{key}")
        return self.objects[key]


def make_s3(objects) -> tuple[S3Input, FakeBucket]:
    s3 = S3Input(endpoint="http://minio:9000", bucket="jw-market-input", access_key="AK", secret_key="SK")
    fake = FakeBucket(objects)
    s3.opener = fake
    return s3, fake


def bucket_objects(tmp_path) -> dict[str, bytes]:
    """Build a contract-shaped bucket from the standard fake submission."""
    root = tmp_path / "seed"
    root.mkdir()
    manifest_path = write_submission(root)
    manifest = json.loads(manifest_path.read_text())
    data_key = manifest["files"][0]["path"]
    return {
        f"_manifests/ubist/2026-07/manifest.json": manifest_path.read_bytes(),
        data_key: (root / data_key).read_bytes(),
    }


def test_read_and_traversal_guard(tmp_path):
    s3, _ = make_s3(bucket_objects(tmp_path))
    assert b"contract_version" in s3.read("_manifests/ubist/2026-07/manifest.json")
    with pytest.raises(S3InputError, match="escapes"):
        s3.read("a/../b")
    with pytest.raises(FileNotFoundError):
        s3.read("nope.json")


def test_list_keys_prefix(tmp_path):
    s3, _ = make_s3(bucket_objects(tmp_path))
    assert s3.list_keys("_manifests/") == ["_manifests/ubist/2026-07/manifest.json"]


def test_webhook_s3_mode_queues_and_launches(tmp_path, sqlite_ledger, fake_transport):
    s3, _ = make_s3(bucket_objects(tmp_path))
    client = TestClient(create_app(IngestService(sqlite_ledger, None, transport=fake_transport, s3=s3)))
    response = client.post(
        "/ingest/webhook", json={"manifest_path": "_manifests/ubist/2026-07/manifest.json"}
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "queued"
    assert len(fake_transport.submitted) == 1
    # missing manifest -> 404, not a crash
    assert client.post("/ingest/webhook", json={"manifest_path": "_manifests/x.json"}).status_code == 404


def test_sweep_s3_mode_kicks_unrecorded(tmp_path, sqlite_ledger, fake_transport):
    s3, _ = make_s3(bucket_objects(tmp_path))
    result = sweep(sqlite_ledger, None, transport=fake_transport, s3=s3)
    assert result["kicked"] == 1
    assert len(fake_transport.submitted) == 1
    # second sweep: identity now queued/running -> no new kick
    result2 = sweep(sqlite_ledger, None, transport=fake_transport, s3=s3)
    assert result2["kicked"] == 0
