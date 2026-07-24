from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest_fixtures import write_submission
from pipeline.scripts.ingest_hook import job_launcher, stage_log_runner, stage_logs
from pipeline.scripts.ingest_hook.app import IngestService, create_app


def _http_409() -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"message": "jobs.batch already exists"}).encode())
    return urllib.error.HTTPError("https://kubernetes/jobs", 409, "Conflict", {}, body)


def test_duplicate_job_error_includes_existing_job_evidence():
    def reject(_path, _body):
        raise _http_409()

    def inspect(_namespace, name):
        assert name.startswith("jw-ingest-ubist-")
        return {
            "metadata": {
                "name": name,
                "creationTimestamp": "2026-07-23T03:20:31Z",
            },
            "status": {
                "conditions": [
                    {"type": "Failed", "status": "True"},
                ],
            },
        }

    with pytest.raises(job_launcher.JobSubmissionConflict) as caught:
        job_launcher.submit_job(
            category="ubist",
            manifest_sha="e" * 64,
            manifest_path="/data/manifest.json",
            namespace="llmops",
            run_id="20260723120000123456",
            transport=reject,
            inspect_transport=inspect,
        )

    assert caught.value.job_name.startswith("jw-ingest-ubist-")
    assert caught.value.existing_status == "Failed"
    assert caught.value.created_at == "2026-07-23T03:20:31Z"
    assert "existing_status=Failed" in str(caught.value)


def test_duplicate_job_error_survives_failed_existing_job_inspection():
    def reject(_path, _body):
        raise _http_409()

    def inspect(_namespace, _name):
        raise urllib.error.HTTPError(
            "https://kubernetes/jobs/existing",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"forbidden"),
        )

    with pytest.raises(job_launcher.JobSubmissionConflict) as caught:
        job_launcher.submit_job(
            category="ubist",
            manifest_sha="e" * 64,
            manifest_path="/data/manifest.json",
            namespace="llmops",
            run_id="20260723120000123456",
            transport=reject,
            inspect_transport=inspect,
        )

    assert caught.value.existing_status == "Unknown"
    assert caught.value.created_at is None
    assert "inspection_error=HTTPError:403" in str(caught.value)


def test_running_submission_conflict_is_recovered_as_idempotent_success(
    sqlite_ledger, bucket
):
    manifest_path = write_submission(bucket)

    def reject(_path, _body):
        raise _http_409()

    def inspect(_namespace, name):
        return {
            "metadata": {
                "name": name,
                "creationTimestamp": "2026-07-23T03:20:31Z",
            },
            "status": {"active": 1},
        }

    service = IngestService(
        sqlite_ledger,
        bucket,
        transport=reject,
        inspect_transport=inspect,
        now=lambda: "20260723120000123456",
    )

    response = service.receive_webhook(str(manifest_path.relative_to(bucket)))

    manifest_sha = next(
        row[0] for row in sqlite_ledger._execute(
            "SELECT manifest_sha FROM ingest_ledger"
        ).fetchall()
    )
    assert response["job_name"].startswith("jw-ingest-ubist-")
    assert sqlite_ledger.status("2026-07", "ubist", manifest_sha).status == "running"
    events = sqlite_ledger.stage_events("2026-07", "ubist", manifest_sha)
    assert len(events) == 1
    assert events[0].run_id == "20260723120000123456"
    assert events[0].stage == "job_submit"
    assert events[0].status == "complete"


def test_log_reader_pages_stage_logs_and_redacts_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIADB_PASSWORD", "db-password-value")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minio-secret-value")
    root = tmp_path / "logs"
    target = stage_logs.stage_log_path(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        "start\n"
        "password=db-password-value\n"
        "Authorization: Bearer abc.def.ghi\n"
        "mysql://user:minio-secret-value@db.example/jw\n"
        "done\n",
        encoding="utf-8",
    )

    first = stage_logs.read_log_page(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
        offset=0,
        limit=30,
    )
    second = stage_logs.read_log_page(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
        offset=first.next_offset,
        limit=4096,
    )
    text = first.text + second.text

    assert "db-password-value" not in text
    assert "minio-secret-value" not in text
    assert "abc.def.ghi" not in text
    assert text.count("[REDACTED]") >= 3
    assert second.truncated is False


def test_log_reader_rejects_logs_larger_than_processing_limit(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "logs"
    target = stage_logs.stage_log_path(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
    )
    target.parent.mkdir(parents=True)
    target.write_text("123456", encoding="utf-8")
    monkeypatch.setattr(stage_logs, "MAX_READABLE_LOG_BYTES", 5)

    with pytest.raises(ValueError, match="log file exceeds readable size limit"):
        stage_logs.read_log_page(
            root,
            job_name="jw-ingest-ubist-eecd1a6a-run1",
            stage="load",
        )


def test_log_reader_redacts_json_and_cli_secret_forms(tmp_path: Path):
    root = tmp_path / "logs"
    target = stage_logs.stage_log_path(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        '{"password":"json-secret","api_key": "json-api-key"}\n'
        "loader --token cli-token --access-key cli-access-key\n",
        encoding="utf-8",
    )

    page = stage_logs.read_log_page(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="load",
    )

    assert "json-secret" not in page.text
    assert "json-api-key" not in page.text
    assert "cli-token" not in page.text
    assert "cli-access-key" not in page.text
    assert page.text.count("[REDACTED]") == 4


def test_log_reader_never_splits_utf8_characters(tmp_path: Path):
    root = tmp_path / "logs"
    target = stage_logs.stage_log_path(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="g3",
    )
    target.parent.mkdir(parents=True)
    target.write_text("시작\n완료\n", encoding="utf-8")

    pages = []
    offset = 0
    while True:
        page = stage_logs.read_log_page(
            root,
            job_name="jw-ingest-ubist-eecd1a6a-run1",
            stage="g3",
            offset=offset,
            limit=4,
        )
        pages.append(page.text)
        assert "\ufffd" not in page.text
        if not page.truncated:
            break
        assert page.next_offset > offset
        offset = page.next_offset

    assert "".join(pages) == "시작\n완료\n"


def test_stage_runner_tees_masked_full_and_stage_logs(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("MARIADB_PASSWORD", "db-password-value")
    root = tmp_path / "logs"
    monkeypatch.setattr(stage_log_runner.config, "log_root", lambda: root)

    class FakeProcess:
        stdout = iter([
            "[stage] g3 start(1/2)\n",
            "password=db-password-value\n",
            "[stage] g3 end rc=0\n",
            "[stage] load skipped reason=test\n",
        ])

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(
        stage_log_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    rc = stage_log_runner.run(
        manifest=tmp_path / "manifest.json",
        run_id="run1",
        job_name="jw-ingest-ubist-eecd1a6a-run1",
    )

    assert rc == 0
    full = stage_logs.full_log_path(
        root, job_name="jw-ingest-ubist-eecd1a6a-run1"
    ).read_text()
    g3 = stage_logs.stage_log_path(
        root, job_name="jw-ingest-ubist-eecd1a6a-run1", stage="g3"
    ).read_text()
    skipped = stage_logs.stage_log_path(
        root, job_name="jw-ingest-ubist-eecd1a6a-run1", stage="load"
    ).read_text()
    assert "db-password-value" not in full
    assert "password=[REDACTED]" in full
    assert "password=[REDACTED]" in g3
    assert "skipped reason=test" in skipped
    captured = capsys.readouterr()
    assert "db-password-value" not in captured.out
    assert "password=[REDACTED]" in captured.out


def test_stage_runner_persists_cgroup_samples_in_mart_build_log(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "logs"
    monkeypatch.setattr(stage_log_runner.config, "log_root", lambda: root)

    class FakeProcess:
        stdout = iter(
            [
                "[stage] mart_build start(4/9)\n",
                "metric=cgroup_memory stage=mart_build sample=periodic "
                "current_bytes=1024 peak_bytes=2048\n",
                "[stage] mart_build end rc=0\n",
            ]
        )

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(
        stage_log_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    assert (
        stage_log_runner.run(
            manifest=tmp_path / "manifest.json",
            run_id="run1",
            job_name="jw-ingest-ubist-eecd1a6a-run1",
        )
        == 0
    )
    mart_log = stage_logs.stage_log_path(
        root,
        job_name="jw-ingest-ubist-eecd1a6a-run1",
        stage="mart_build",
    ).read_text(encoding="utf-8")
    assert "current_bytes=1024 peak_bytes=2048" in mart_log


def test_log_api_returns_success_and_explicit_missing_reason(
    sqlite_ledger, bucket, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("INGEST_LOG_ROOT", str(tmp_path / "logs"))
    manifest_path = write_submission(bucket)
    service = IngestService(
        sqlite_ledger,
        bucket,
        transport=lambda _path, _body: {},
        now=lambda: "20260723120000123456",
    )
    payload = service.receive_webhook(str(manifest_path.relative_to(bucket)))
    run_id = "20260723120000123456"
    job_name = job_launcher.job_name("ubist", payload["manifest_sha"], run_id)
    sqlite_ledger.record_stage(
        payload["epoch"],
        "ubist",
        payload["manifest_sha"],
        run_id=run_id,
        seq=1,
        stage="g3",
        status="complete",
    )
    log_path = stage_logs.stage_log_path(
        tmp_path / "logs", job_name=job_name, stage="g3"
    )
    log_path.parent.mkdir(parents=True)
    log_path.write_text("gate=g3 status=pass\n", encoding="utf-8")
    client = TestClient(create_app(service))

    response = client.get(
        "/ingest/logs",
        params={
            "epoch": payload["epoch"],
            "category": "ubist",
            "manifest_sha": payload["manifest_sha"],
            "run_id": run_id,
            "stage": "g3",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "gate=g3 status=pass\n"
    assert body["truncated"] is False

    missing = client.get(
        "/ingest/logs",
        params={
            "epoch": payload["epoch"],
            "category": "ubist",
            "manifest_sha": payload["manifest_sha"],
            "run_id": run_id,
            "stage": "load",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["reason"] == "log_not_available"
