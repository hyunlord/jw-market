from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest_fixtures import write_submission
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.test_runs import TestRunStore


@pytest.fixture(autouse=True)
def _test_source_contract(monkeypatch):
    monkeypatch.setenv("INGEST_TEST_SOURCE_DB_HOST", "reader.internal")
    monkeypatch.setenv("INGEST_TEST_SOURCE_DB_NAME", "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv("INGEST_TEST_SOURCE_CORPUS_ROOT", "/market-output/ubist")
    monkeypatch.setenv(
        "INGEST_TEST_SOURCE_CATALOG_ROOT",
        "/market-output/shadow/catalog",
    )


def _service(
    sqlite_ledger,
    bucket: Path,
    fake_transport,
    tmp_path: Path,
    *,
    inspect_transport=None,
    delete_transport=None,
) -> IngestService:
    return IngestService(
        sqlite_ledger,
        bucket,
        transport=fake_transport,
        inspect_transport=inspect_transport,
        delete_transport=delete_transport,
        test_run_store=TestRunStore(tmp_path / "test-runs"),
        now=lambda: "20260727010101000000",
        sleep=lambda _seconds: None,
    )


def test_capabilities_support_only_completed_ubist_path(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    response = TestClient(
        create_app(_service(sqlite_ledger, bucket, fake_transport, tmp_path))
    ).get("/ingest/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["test_load"]["ubist"]["supported"] is True
    for category in ("iqvia_nsa", "iqvia_csd_channel", "iqvia_csd_keyword", "mi_master"):
        assert payload["test_load"][category]["supported"] is False
        assert payload["test_load"][category]["reason"]


def test_duplicate_manifest_creates_distinct_test_run_ids_without_ledger_identity(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    manifest = write_submission(bucket)
    service = _service(sqlite_ledger, bucket, fake_transport, tmp_path)
    client = TestClient(create_app(service))

    first = client.post(
        "/ingest/test-runs",
        json={"manifest_path": str(manifest), "requested_by": "pl@example.test"},
    )
    assert first.status_code == 202
    first_payload = first.json()
    service.test_run_store.update(first_payload["run_id"], status="completed")

    second = client.post(
        "/ingest/test-runs",
        json={"manifest_path": str(manifest), "requested_by": "pl@example.test"},
    )
    assert second.status_code == 202
    assert second.json()["run_id"] != first_payload["run_id"]

    parsed = service._read_manifest(str(manifest))[0]
    assert sqlite_ledger.status(parsed.epoch, parsed.category, parsed.manifest_sha) is None
    assert len(fake_transport.submitted) == 2


def test_test_run_is_rejected_while_production_identity_is_queued(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    manifest = write_submission(bucket)
    parsed = _service(
        sqlite_ledger, bucket, fake_transport, tmp_path
    )._read_manifest(str(manifest))[0]
    sqlite_ledger.receive(
        parsed.epoch,
        parsed.category,
        parsed.manifest_sha,
        manifest_path=str(manifest),
    )
    client = TestClient(
        create_app(_service(sqlite_ledger, bucket, fake_transport, tmp_path))
    )

    response = client.post(
        "/ingest/test-runs",
        json={"manifest_path": str(manifest), "requested_by": "pl@example.test"},
    )

    assert response.status_code == 409
    assert "production" in response.json()["detail"]
    assert fake_transport.submitted == []


def test_production_promotion_marks_an_active_test_preview_stale(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    manifest = write_submission(bucket)
    service = _service(sqlite_ledger, bucket, fake_transport, tmp_path)
    active = service.test_run_store.create(
        category="ubist",
        epoch="2026-07",
        manifest_sha="b" * 64,
        manifest_path="/input/test-manifest.json",
        requested_by="pl@example.test",
    )
    service.test_run_store.update(active.run_id, status="running")

    service.receive_webhook(str(manifest))

    stale = service.test_run_store.get(active.run_id)
    assert stale is not None
    assert stale.stale_preview is True
    assert "snapshot" in (stale.reason or "")


def test_test_run_status_and_cancel_use_test_identity_only(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    manifest = write_submission(bucket)
    inspected = 0
    deleted = []

    def inspect(_namespace, name):
        nonlocal inspected
        inspected += 1
        if inspected == 1:
            return {
                "metadata": {
                    "name": name,
                    "uid": "test-job-uid",
                    "resourceVersion": "42",
                },
                "status": {"active": 1},
            }
        raise urllib.error.HTTPError(
            url=f"https://kubernetes/jobs/{name}",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    service = _service(
        sqlite_ledger,
        bucket,
        fake_transport,
        tmp_path,
        inspect_transport=inspect,
        delete_transport=lambda path, body: deleted.append((path, body)),
    )
    client = TestClient(create_app(service))
    created = client.post(
        "/ingest/test-runs",
        json={"manifest_path": str(manifest), "requested_by": "pl@example.test"},
    ).json()

    status = client.get(f"/ingest/test-runs/{created['run_id']}")
    assert status.status_code == 200
    assert status.json()["run_id"] == created["run_id"]
    assert status.json()["status"] == "queued"

    cancelled = client.post(
        f"/ingest/test-runs/{created['run_id']}/cancel",
        json={"requested_by": "pl@example.test"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert inspected == 2
    assert len(deleted) == 1


def test_absent_job_is_marked_failed_before_next_test_run(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    manifest = write_submission(bucket)

    def inspect(_namespace, name):
        raise urllib.error.HTTPError(
            url=f"https://kubernetes/jobs/{name}",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    service = _service(
        sqlite_ledger,
        bucket,
        fake_transport,
        tmp_path,
        inspect_transport=inspect,
    )
    first = service.test_run_store.create(
        category="ubist",
        epoch="2026-07",
        manifest_sha="b" * 64,
        manifest_path="/input/old-manifest.json",
        requested_by="pl@example.test",
    )
    service.test_run_store.update(
        first.run_id,
        status="running",
        job_name="jw-ingest-test-orphan",
    )

    response = TestClient(create_app(service)).post(
        "/ingest/test-runs",
        json={"manifest_path": str(manifest), "requested_by": "pl@example.test"},
    )

    assert response.status_code == 202
    stale = service.test_run_store.get(first.run_id)
    assert stale is not None
    assert stale.status == "failed"
    assert "absent" in (stale.reason or "").lower()
