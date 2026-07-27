"""/ingest/status must distinguish "no observation" from "observation unreadable".

Covers the C-4 gates:
  C-1 read succeeds            -> observation_available=true, rows present
  C-2 observation table absent -> observation_available=false + reason, entry kept
  C-3 genuinely zero rows      -> observation_available=true, empty list
  C-4 only the signal table absent -> reproduces the live production condition and
      attributes the failure to signal_events specifically

Before this, a failed read and an empty result were the same response body, so the
site could not tell "this run recorded nothing" from "we cannot read what it
recorded". The endpoint still returns 200 with the ledger entry: the entry is
readable and callers depend on it, so a broken observation table must not take the
whole status endpoint down.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from ingest_fixtures import write_submission

IDENTITY = ("2026-07", "ubist")

_ORIGINAL_KEYS = (
    "epoch", "category", "manifest_sha", "status", "reason", "job_name",
    "uploaded_by", "received_at", "finished_at",
)


@pytest.fixture
def service(sqlite_ledger, bucket, fake_transport) -> IngestService:
    return IngestService(sqlite_ledger, bucket, transport=fake_transport)


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service))


def _sha(manifest_path) -> str:
    from pipeline.scripts.ingest_hook.contract import load_manifest

    return load_manifest(manifest_path).manifest_sha


def _register(service, bucket) -> str:
    """Create a ledger entry without recording any observation rows."""
    manifest_path = write_submission(bucket)
    sha = _sha(manifest_path)
    service.ledger.receive(*IDENTITY, sha, manifest_path=str(manifest_path))
    return sha


def _get(client, sha) -> dict:
    response = client.get(
        "/ingest/status",
        params={"epoch": IDENTITY[0], "category": IDENTITY[1], "manifest_sha": sha},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_c1_successful_observation_read_is_reported_available(service, client, bucket, tmp_path):
    manifest_path = write_submission(bucket)
    sha = _sha(manifest_path)
    service.ledger.receive(*IDENTITY, sha, manifest_path=str(manifest_path))
    job_runner.run(
        manifest_path, input_root=bucket, ledger=service.ledger, rehearsal_root=tmp_path / "s"
    )

    body = _get(client, sha)
    assert body["observation_available"] is True
    assert body["observation_error"] is None
    assert body["stages"] and body["signals"]


def test_c2_missing_stage_table_is_reported_not_hidden(service, client, bucket):
    sha = _register(service, bucket)
    service.ledger._execute("DROP TABLE ingest_stage_event")

    body = _get(client, sha)
    assert body["observation_available"] is False
    assert "stage_events" in body["observation_error"]
    assert "ingest_stage_event" in body["observation_error"]
    assert body["stages"] == []
    # the ledger entry itself is still served, unchanged
    for key in _ORIGINAL_KEYS:
        assert key in body, key
    assert body["status"] == "queued"


def test_c3_genuinely_empty_observation_stays_available(service, client, bucket):
    sha = _register(service, bucket)

    body = _get(client, sha)
    assert body["observation_available"] is True
    assert body["observation_error"] is None
    assert body["stages"] == []
    assert body["signals"] == []


def test_c4_missing_signal_table_is_attributed_to_signal_events(service, client, bucket):
    """The live 2026-07-27 condition: stage table present, signal table absent."""
    sha = _register(service, bucket)
    service.ledger._execute("DROP TABLE ingest_signal_event")

    body = _get(client, sha)
    assert body["observation_available"] is False
    assert "signal_events" in body["observation_error"]
    assert "stage_events" not in body["observation_error"]
    assert body["signals"] == []
    assert body["stages"] == []  # stage table is fine, this run simply has no rows


def test_c5_original_keys_are_untouched_and_only_additive_keys_appeared(service, client, bucket):
    sha = _register(service, bucket)
    body = _get(client, sha)
    for key in _ORIGINAL_KEYS:
        assert key in body, key
    assert set(body) == set(_ORIGINAL_KEYS) | {
        "observation_available", "observation_error", "current_stage", "stages",
        "signals", "log_ref",
        # Ledger provenance: which ledger answered, and what the other one says.
        # _ORIGINAL_KEYS above stays untouched — this set is the allow-list of
        # ADDED keys, so a removal or rename of an original key still fails here.
        "ledger_source", "ledger_bound", "counterpart_source",
        "counterpart_available", "counterpart_error", "counterpart_status",
        "counterpart_finished_at", "ledgers_agree",
    }
