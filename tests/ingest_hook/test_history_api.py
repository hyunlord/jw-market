from __future__ import annotations

from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.source_inventory import (
    FileObservation,
    ScanSnapshot,
    write_inventory_snapshot,
)


EPOCH = "2026-06"
CATEGORY = "ubist"
MANIFEST_SHA = "a" * 64
BUILD_RUN_ID = "20260806155944833982"
PUBLISH_RUN_ID = "20260806220035453546"


def _client(sqlite_ledger, bucket, fake_transport, *, inventory_root=None) -> TestClient:
    service = IngestService(
        sqlite_ledger,
        bucket,
        transport=fake_transport,
        inventory_root=inventory_root,
    )
    return TestClient(create_app(service))


def test_history_includes_ledger_run_and_stage_only_publish_run(
    sqlite_ledger, bucket, fake_transport
):
    sqlite_ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    sqlite_ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name="jw-ingest-ubist-build",
        run_id=BUILD_RUN_ID,
    )
    sqlite_ledger.record_stage(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=BUILD_RUN_ID,
        seq=1,
        stage="g3",
        status="complete",
        started_at="2026-08-06T15:59:44Z",
        finished_at="2026-08-06T16:00:44Z",
        duration_ms=60_000,
    )
    sqlite_ledger.record_stage(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=PUBLISH_RUN_ID,
        seq=1,
        stage="mart_publish",
        status="failed",
        reason="publish stopped before atomic rename",
        started_at="2026-08-06T22:00:35Z",
        finished_at="2026-08-06T22:10:21Z",
        duration_ms=586_000,
    )

    response = _client(sqlite_ledger, bucket, fake_transport).get(
        "/ingest/history", params={"limit": 100}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_offset"] is None
    assert {
        (item["epoch"], item["category"], item["manifest_sha"], item["run_id"])
        for item in payload["items"]
    } == {
        (EPOCH, CATEGORY, MANIFEST_SHA, BUILD_RUN_ID),
        (EPOCH, CATEGORY, MANIFEST_SHA, PUBLISH_RUN_ID),
    }
    by_run = {item["run_id"]: item for item in payload["items"]}
    assert by_run[BUILD_RUN_ID]["ledger"] == {
        "status": "running",
        "reason": None,
        "job_name": "jw-ingest-ubist-build",
        "uploaded_by": None,
        "received_at": by_run[BUILD_RUN_ID]["ledger"]["received_at"],
        "started_at": by_run[BUILD_RUN_ID]["ledger"]["started_at"],
        "finished_at": None,
    }
    assert [event["stage"] for event in by_run[BUILD_RUN_ID]["stages"]] == ["g3"]
    assert by_run[PUBLISH_RUN_ID]["ledger"] is None
    assert by_run[PUBLISH_RUN_ID]["stages"][0]["status"] == "failed"


def test_history_adds_identity_evidence_without_collapsing_runs(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    inventory_root = tmp_path / "inventory"
    sqlite_ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    sqlite_ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name="jw-ingest-ubist-build",
        run_id=BUILD_RUN_ID,
    )
    sqlite_ledger.record_stage(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=BUILD_RUN_ID,
        seq=1,
        stage="load",
        status="complete",
        started_at="2026-08-06T16:00:00Z",
        finished_at="2026-08-06T16:01:00Z",
    )
    sqlite_ledger.record_stage(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=PUBLISH_RUN_ID,
        seq=1,
        stage="mart_publish",
        status="complete",
        started_at="2026-08-06T22:00:35Z",
        finished_at="2026-08-06T22:10:21Z",
    )
    sqlite_ledger.mark_complete(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        row_counts={"epoch:2026-06": 2_043_451, "source:a.xlsx": 808_635},
    )
    write_inventory_snapshot(
        ScanSnapshot(
            schema_version="1",
            category=CATEGORY,
            epoch=EPOCH,
            manifest_sha=MANIFEST_SHA,
            run_id=BUILD_RUN_ID,
            observed_at="2026-08-06T16:00:00Z",
            files=(
                FileObservation(
                    relative_path="production/source.xlsx",
                    sha256="b" * 64,
                    size=123,
                    state="classified",
                    category=CATEGORY,
                    rows=456,
                    periods=("2023-01", "2026-05"),
                ),
                FileObservation(
                    relative_path="._source.xlsx",
                    sha256="c" * 64,
                    size=82,
                    state="excluded",
                    reason="AppleDouble metadata",
                ),
            ),
        ),
        inventory_root,
    )

    response = _client(
        sqlite_ledger,
        bucket,
        fake_transport,
        inventory_root=inventory_root,
    ).get("/ingest/history", params={"limit": 100})

    assert response.status_code == 200
    by_run = {item["run_id"]: item for item in response.json()["items"]}
    assert set(by_run) == {BUILD_RUN_ID, PUBLISH_RUN_ID}
    assert [event["stage"] for event in by_run[BUILD_RUN_ID]["stages"]] == ["load"]
    assert [event["stage"] for event in by_run[PUBLISH_RUN_ID]["stages"]] == [
        "mart_publish"
    ]
    for item in by_run.values():
        assert item["row_counts"] == {
            "epoch:2026-06": 2_043_451,
            "source:a.xlsx": 808_635,
        }
        assert item["inventory_run_id"] == BUILD_RUN_ID
        assert item["file_count"] == 2
        assert item["classified_file_count"] == 1
        assert item["manifest_file_count"] == 1
        assert item["inventory_file_count"] == 2
        assert item["execution_period_from"] == "2023-01"
        assert item["execution_period_to"] == "2026-05"
        assert [
            (event["run_id"], event["stage"], event["status"])
            for event in item["identity_stages"]
        ] == [
            (BUILD_RUN_ID, "load", "complete"),
            (PUBLISH_RUN_ID, "mart_publish", "complete"),
        ]

    status = _client(
        sqlite_ledger,
        bucket,
        fake_transport,
        inventory_root=inventory_root,
    ).get(
        "/ingest/status",
        params={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
        },
    )
    assert status.status_code == 200
    assert status.json()["row_counts"] == {
        "epoch:2026-06": 2_043_451,
        "source:a.xlsx": 808_635,
    }
    assert status.json()["inventory_run_id"] == BUILD_RUN_ID
    assert status.json()["file_count"] == 2
    assert status.json()["classified_file_count"] == 1
    assert status.json()["manifest_file_count"] == 1
    assert status.json()["inventory_file_count"] == 2
    assert status.json()["execution_period_from"] == "2023-01"
    assert status.json()["execution_period_to"] == "2026-05"


def test_history_keeps_prior_run_from_transition_when_stage_record_is_absent(
    sqlite_ledger, bucket, fake_transport
):
    first_run = "run-before-stage-recording-failed"
    second_run = "run-after-requeue"
    sqlite_ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    sqlite_ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name="jw-ingest-first",
        run_id=first_run,
    )
    sqlite_ledger.mark_failed(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        reason="stage_event write failed before the first observable stage",
    )
    sqlite_ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    sqlite_ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name="jw-ingest-second",
        run_id=second_run,
    )

    response = _client(sqlite_ledger, bucket, fake_transport).get(
        "/ingest/history", params={"limit": 100}
    )

    assert response.status_code == 200
    by_run = {item["run_id"]: item for item in response.json()["items"]}
    assert {first_run, second_run} <= set(by_run)
    assert by_run[first_run]["ledger"] is None
    assert by_run[first_run]["stages"] == []
    assert by_run[second_run]["ledger"]["status"] == "running"


def test_history_paginates_complete_runs_without_splitting_stage_rows(
    sqlite_ledger, bucket, fake_transport
):
    for index in range(3):
        manifest_sha = f"{index + 1:064x}"
        run_id = f"run-{index}"
        sqlite_ledger.receive(
            EPOCH,
            CATEGORY,
            manifest_sha,
            manifest_path=f"_manifests/ubist/{index}/manifest.json",
        )
        sqlite_ledger.mark_running(
            EPOCH,
            CATEGORY,
            manifest_sha,
            job_name=f"job-{index}",
            run_id=run_id,
        )
        for seq, stage in enumerate(("g3", "load"), start=1):
            sqlite_ledger.record_stage(
                EPOCH,
                CATEGORY,
                manifest_sha,
                run_id=run_id,
                seq=seq,
                stage=stage,
                status="complete",
                started_at=f"2026-08-06T0{index}:00:0{seq}Z",
                finished_at=f"2026-08-06T0{index}:00:1{seq}Z",
            )

    client = _client(sqlite_ledger, bucket, fake_transport)
    first = client.get("/ingest/history", params={"limit": 2}).json()
    second = client.get(
        "/ingest/history", params={"limit": 2, "offset": first["next_offset"]}
    ).json()

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["next_offset"] == 2
    assert second["next_offset"] is None
    items = first["items"] + second["items"]
    assert len({item["run_id"] for item in items}) == 3
    assert all([event["stage"] for event in item["stages"]] == ["g3", "load"] for item in items)


def test_inventory_detail_is_loaded_lazily_by_exact_run_identity(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    snapshot = ScanSnapshot(
        schema_version="1",
        category=CATEGORY,
        epoch=EPOCH,
        manifest_sha=MANIFEST_SHA,
        run_id=BUILD_RUN_ID,
        observed_at="2026-08-07T00:00:00Z",
        files=(
            FileObservation(
                relative_path="production/source.xlsx",
                sha256="b" * 64,
                size=123,
                state="classified",
                category=CATEGORY,
                rows=456,
                periods=("2026-05", "2026-06"),
            ),
        ),
    )
    inventory_root = tmp_path / "inventory"
    write_inventory_snapshot(snapshot, inventory_root)
    client = _client(
        sqlite_ledger,
        bucket,
        fake_transport,
        inventory_root=inventory_root,
    )

    response = client.get(
        "/ingest/inventory",
        params={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": BUILD_RUN_ID,
        },
    )

    assert response.status_code == 200
    assert response.json()["files"][0]["relative_path"] == "production/source.xlsx"
    missing = client.get(
        "/ingest/inventory",
        params={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": "missing-run",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "inventory snapshot not recorded"


def test_inventory_detail_rejects_path_shaped_identity_values(
    sqlite_ledger, bucket, fake_transport, tmp_path
):
    client = _client(
        sqlite_ledger,
        bucket,
        fake_transport,
        inventory_root=tmp_path / "inventory",
    )

    response = client.get(
        "/ingest/inventory",
        params={
            "epoch": "../2026-06",
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": BUILD_RUN_ID,
        },
    )

    assert response.status_code == 422
