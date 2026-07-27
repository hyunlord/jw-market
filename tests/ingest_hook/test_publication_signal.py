from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook import publication_signal
from pipeline.scripts.ingest_hook.publication_signal import (
    NORMAL_CACHE_TABLES,
    build_provenance,
    publish_completion,
)


def test_publish_completion_dry_run_plans_epoch_cache_and_notifications() -> None:
    calls = []

    def connection_factory():
        calls.append("called")
        raise AssertionError("dry_run must not open a DB connection")

    result = publish_completion(
        "ubist",
        "2026-07",
        "run-1",
        connection_factory=connection_factory,
        dry_run=True,
    )

    assert calls == []
    assert result.status == "planned"
    assert result.mart_publication_epoch is None
    assert result.cache_invalidation.tables == NORMAL_CACHE_TABLES
    assert "cache_cause" not in result.cache_invalidation.tables
    assert "cache_deep_analysis" not in result.cache_invalidation.tables
    assert result.dashboard_payload["event"] == "mart_publication_planned"
    assert result.chat_payload["category"] == "ubist"


def test_publish_completion_bumps_epoch_atomically_with_injected_connection() -> None:
    conn = sqlite3.connect(":memory:")

    first = publish_completion(
        "ubist",
        "2026-07",
        "run-1",
        connection_factory=lambda: conn,
    )
    second = publish_completion(
        "ubist",
        "2026-08",
        "run-2",
        connection_factory=lambda: conn,
    )

    assert first.status == "recorded"
    assert first.mart_publication_epoch == 1
    assert second.mart_publication_epoch == 2
    row = conn.execute(
        "SELECT mart_publication_epoch, category, epoch, run_id "
        "FROM ingest_publication_state WHERE name='normal_caches'"
    ).fetchone()
    assert row == (2, "ubist", "2026-08", "run-2")


def test_publish_completion_uses_declared_epoch_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    monkeypatch.setenv(
        publication_signal.ENV_PUBLICATION_EPOCH_TABLE,
        "mart_publication_epoch",
    )

    publish_completion(
        "iqvia_nsa",
        "2026-Q1",
        "run-1",
        connection_factory=lambda: conn,
    )

    row = conn.execute(
        "SELECT category, epoch, run_id FROM mart_publication_epoch"
    ).fetchone()
    assert row == ("iqvia_nsa", "2026-Q1", "run-1")


def test_publish_completion_rejects_unsafe_epoch_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        publication_signal.ENV_PUBLICATION_EPOCH_TABLE,
        "state; DROP TABLE serving",
    )

    with pytest.raises(ValueError, match="SQL identifier"):
        publish_completion(
            "ubist",
            "2026-07",
            "run-1",
            connection_factory=lambda: sqlite3.connect(":memory:"),
        )


def test_publish_completion_result_serializes_for_status_payload() -> None:
    result = publish_completion(
        "ubist",
        "2026-07",
        "run-1",
        connection_factory=lambda: sqlite3.connect(":memory:"),
    )

    payload = result.as_status_payload()

    assert payload["stage"] == "publication_signal"
    assert payload["status"] == "recorded"
    assert payload["reason"] is None
    assert payload["mart_publication_epoch"] == 1
    assert payload["cache_invalidation"]["tables"] == list(NORMAL_CACHE_TABLES)
    assert payload["notifications"]["dashboard"]["mart_publication_epoch"] == 1


def test_job_runner_exposes_publish_completion_wrapper(monkeypatch) -> None:
    calls = []

    def fake_publish_completion(
        category,
        epoch,
        run_id,
        *,
        connection_factory=None,
        dry_run=False,
        provenance=None,
    ):
        calls.append(
            (category, epoch, run_id, connection_factory, dry_run, provenance)
        )
        return "published"

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.publication_signal.publish_completion",
        fake_publish_completion,
    )

    result = job_runner.publish_completion(
        "ubist",
        "2026-07",
        "run-1",
        connection_factory="factory",
        dry_run=True,
        provenance="lineage",
    )

    assert result == "published"
    assert calls == [
        ("ubist", "2026-07", "run-1", "factory", True, "lineage")
    ]


@dataclass(frozen=True)
class _ManifestFile:
    path: str
    sha256: str
    rows: int | None = None


def test_provenance_is_order_independent_and_queryable() -> None:
    files = [
        _ManifestFile("b.xlsx", "b" * 64, 20),
        _ManifestFile("a.xlsx", "a" * 64, 10),
    ]
    provenance = build_provenance(
        files,
        file_rows={"a.xlsx": 11, "b.xlsx": 22},
        periods={"2026-02", "2026-01"},
        builder_commit="1234567",
        image_digest="repo/image@sha256:" + ("c" * 64),
    )
    reordered = build_provenance(
        list(reversed(files)),
        file_rows={"b.xlsx": 22, "a.xlsx": 11},
        periods={"2026-01", "2026-02"},
        builder_commit="1234567",
        image_digest="repo/image@sha256:" + ("c" * 64),
    )
    assert provenance.inventory_sha256 == reordered.inventory_sha256
    assert provenance.inventory_json == reordered.inventory_json
    assert provenance.window_start == "2026-01"
    assert provenance.window_end == "2026-02"

    conn = sqlite3.connect(":memory:")
    publish_completion(
        "ubist",
        "2026-02",
        "run-provenance",
        connection_factory=lambda: conn,
        provenance=provenance,
    )

    row = conn.execute(
        "SELECT category, input_inventory_sha256, input_inventory_json, "
        "builder_commit, image_digest, window_start, window_end "
        "FROM mart_publication_provenance"
    ).fetchone()
    assert row == (
        "ubist",
        provenance.inventory_sha256,
        provenance.inventory_json,
        "1234567",
        "repo/image@sha256:" + ("c" * 64),
        "2026-01",
        "2026-02",
    )


def test_provenance_rejects_builder_commit_that_differs_from_image_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_commit = "a" * 40
    monkeypatch.setenv("APP_VERSION", image_commit)
    monkeypatch.setenv("BUILD_GIT_SHA", "b" * 40)

    with pytest.raises(ValueError, match="does not match image APP_VERSION"):
        build_provenance(
            [_ManifestFile("a.xlsx", "c" * 64, 1)],
            file_rows={"a.xlsx": 1},
            periods={"2026-01"},
            image_digest="repo/image@sha256:" + ("d" * 64),
        )


def test_provenance_rejects_explicit_harness_commit_that_differs_from_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", "a" * 40)
    monkeypatch.delenv("BUILD_GIT_SHA", raising=False)

    with pytest.raises(ValueError, match="builder_commit does not match"):
        build_provenance(
            [_ManifestFile("a.xlsx", "c" * 64, 1)],
            file_rows={"a.xlsx": 1},
            periods={"2026-01"},
            builder_commit="b" * 40,
            image_digest="repo/image@sha256:" + ("d" * 64),
        )


def test_provenance_uses_image_version_as_builder_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_commit = "a" * 40
    monkeypatch.setenv("APP_VERSION", image_commit)
    monkeypatch.delenv("BUILD_GIT_SHA", raising=False)
    monkeypatch.delenv("R1_GIT_COMMIT", raising=False)

    provenance = build_provenance(
        [_ManifestFile("a.xlsx", "c" * 64, 1)],
        file_rows={"a.xlsx": 1},
        periods={"2026-01"},
        image_digest="repo/image@sha256:" + ("d" * 64),
    )

    assert provenance.builder_commit == image_commit


def test_same_provenance_retry_reuses_publication_epoch() -> None:
    provenance = build_provenance(
        [_ManifestFile("a.xlsx", "a" * 64, 1)],
        file_rows={"a.xlsx": 1},
        periods={"2026-01"},
        builder_commit="1234567",
        image_digest="repo/image@sha256:" + ("c" * 64),
    )
    conn = sqlite3.connect(":memory:")

    first = publish_completion(
        "ubist",
        "2026-01",
        "run-1",
        connection_factory=lambda: conn,
        provenance=provenance,
    )
    retry = publish_completion(
        "ubist",
        "2026-01",
        "run-2",
        connection_factory=lambda: conn,
        provenance=provenance,
    )

    assert retry.mart_publication_epoch == first.mart_publication_epoch
    assert conn.execute(
        "SELECT COUNT(*) FROM mart_publication_provenance"
    ).fetchone() == (1,)


def test_provenance_table_name_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        publication_signal.ENV_PUBLICATION_PROVENANCE_TABLE,
        "lineage; DROP TABLE serving",
    )
    provenance = build_provenance(
        [_ManifestFile("a.xlsx", "a" * 64, 1)],
        file_rows={"a.xlsx": 1},
        periods={"2026-01"},
        builder_commit="1234567",
        image_digest="repo/image@sha256:" + ("c" * 64),
    )

    with pytest.raises(ValueError, match="SQL identifier"):
        publish_completion(
            "ubist",
            "2026-01",
            "run-1",
            connection_factory=lambda: sqlite3.connect(":memory:"),
            provenance=provenance,
        )


def test_provenance_rejects_mutable_or_missing_image_identity() -> None:
    common = {
        "files": [_ManifestFile("a.xlsx", "a" * 64, 1)],
        "file_rows": {"a.xlsx": 1},
        "periods": {"2026-01"},
        "builder_commit": "1234567",
    }

    with pytest.raises(ValueError, match="immutable ingest image"):
        build_provenance(**common, image_digest="")
    with pytest.raises(ValueError, match="immutable ingest image"):
        build_provenance(**common, image_digest="repo/image:latest")


def test_job_runner_records_publication_payload_for_status(
    tmp_path,
) -> None:
    from pipeline.scripts.ingest_hook.ledger import open_sqlite_ledger

    status_ledger = open_sqlite_ledger(tmp_path / "ledger.sqlite")
    identity = ("2026-01", "ubist", "a" * 64)
    status_ledger.receive(
        *identity,
        manifest_path="/input/manifest.json",
        uploaded_by="tester",
    )
    result = publish_completion(
        "ubist",
        "2026-01",
        "run-1",
        connection_factory=lambda: sqlite3.connect(":memory:"),
    )

    job_runner._record_publication_status(
        ledger=status_ledger,
        identity=identity,
        run_id="run-1",
        mode="production",
        rows_loaded=7,
        publication_result=result,
    )

    events = status_ledger.signal_events(*identity)
    assert len(events) == 1
    assert events[0].event == "publication"
    assert events[0].payload["mart_publication_epoch"] == 1
    assert events[0].payload["notifications"]["chat"]["category"] == "ubist"
