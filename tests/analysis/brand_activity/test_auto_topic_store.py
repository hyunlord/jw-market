from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import topic_store
from pipeline.scripts.analysis.brand_activity.auto_topic import topic_store_db
from pipeline.scripts.analysis.brand_activity.auto_topic import verification
from pipeline.scripts.analysis.brand_activity.auto_topic.audit import write_json


def _artifact_payload() -> topic_store.TopicArtifacts:
    """Create a tiny measured payload shaped like the latest top-7 run."""
    return topic_store.TopicArtifacts(
        run_summary={
            "tag": "serving_direct_singleconcept_top7_exec_20260620_143124",
            "input_fingerprint": "fp-current",
            "market_count": 1,
            "sampled_brand_count": 2,
            "quality_grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0},
        },
        verification={
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "estimated_usd_vertex_flash_proxy": 0.1234,
            "complex_label_count": 0,
            "brand_specific_duplicate_pair_count": 0,
            "serving_route": {"backend": "direct_serving", "model_id": "genos-flash"},
        },
        viz_payload={
            "markets": [
                {
                    "scope_id": "atc4:A02B2",
                    "scope_key": "A02B2",
                    "display_name": "PPI Market",
                    "atc4_values": ["A02B2"],
                    "quality_grade": "A",
                    "axis_row_count": 11403,
                }
            ],
            "brand_results": [
                {
                    "sample_key": "A02B2:JAQBO",
                    "scope_id": "atc4:A02B2",
                    "scope_key": "A02B2",
                    "brand": "JAQBO",
                    "atc4": "A02B2",
                    "row_count": 100,
                    "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 91, "share_pct": 91.0}],
                    "brand_specific_topics": [{"topic_id": "B1", "label": "보험", "affected_row_count": 4, "share_pct": 4.0}],
                },
                {
                    "sample_key": "A02B2:RABEKHAN",
                    "scope_id": "atc4:A02B2",
                    "scope_key": "A02B2",
                    "brand": "RABEKHAN",
                    "atc4": "A02B2",
                    "row_count": 20,
                    "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 20, "share_pct": 100.0}],
                    "brand_specific_topics": [],
                },
            ],
        },
        axis_results={
            "A02B2": {
                "scope_id": "atc4:A02B2",
                "source_row_count": 11403,
                "topics": [{"topic_id": "T1", "label": "효능", "definition": "효능", "keywords": []}],
            },
            "A02B2:pro": {
                "scope_id": "atc4:A02B2",
                "topics": [{"topic_id": "P1", "label": "pro 제외", "definition": "", "keywords": []}],
            },
        },
    )


def test_build_topic_records_keeps_primary_payload_only() -> None:
    """Given latest-style artifacts, When records are built, Then only primary market payload is emitted."""
    artifacts = _artifact_payload()

    records = topic_store.build_topic_records(artifacts)

    assert len(records) == 1
    assert records[0].scope_id == "atc4:A02B2"
    assert records[0].brand_count == 2
    assert records[0].payload["axis"]["topics"][0]["topic_id"] == "T1"
    assert records[0].payload["axis"]["topics"][0]["label"] == "효능"
    assert records[0].payload["brands"][0]["brand"] == "JAQBO"
    assert all("pro 제외" not in str(record.payload) for record in records)


def test_topic_payload_sample_uses_stored_axis_and_brand_topic_shapes() -> None:
    """Given stored mart payload JSON, When sampled for audit, Then real topics are not reported empty."""
    records = topic_store.build_topic_records(_artifact_payload())

    sample = topic_store.topic_payload_sample(records[0].payload)

    assert sample["axis_topics"] == [{"topic_id": "T1", "label": "효능"}]
    assert sample["brand_topics"][0]["topic_shares"] == [
        {"topic_id": "T1", "label": "효능", "share_pct": 91.0}
    ]
    assert sample["brand_topics"][0]["brand_specific_topics"] == [
        {"topic_id": "B1", "label": "보험", "share_pct": 4.0}
    ]


def test_build_run_record_uses_measured_verification_totals() -> None:
    """Given verification evidence, When run metadata is built, Then token and quality totals survive."""
    artifacts = _artifact_payload()

    run = topic_store.build_run_record(
        artifacts,
        artifact_sha256="ede1a742e4f700db00e6e091528bcb7926435bf6d4207d0a1b1afec35d0567f7",
    )

    assert run.run_id == "serving_direct_singleconcept_top7_exec_20260620_143124"
    assert run.model_id == "genos-flash"
    assert run.route == "direct_serving"
    assert run.total_prompt_tokens == 100
    assert run.total_completion_tokens == 25
    assert run.market_count == 1
    assert run.brand_count == 2
    assert run.axis_compound_count == 0
    assert run.brand_specific_dup_count == 0
    assert run.input_fingerprint == "fp-current"


def test_build_run_record_parses_replay_tag_timestamp_and_db_snapshot_fingerprint() -> None:
    """Given recovery artifacts, When run metadata is built, Then created_at and guard seed survive."""
    artifacts = _artifact_payload()
    artifacts.run_summary.pop("input_fingerprint")
    artifacts.run_summary["tag"] = "brand_activity_replay_20260702_160109"
    artifacts.db_snapshot.update(
        {
            "before": {
                "stage_hash_fingerprint": "ecb7c06943b7d44fffde4e8761f281546237148d1f10ed088017d96e584fb135"
            }
        }
    )

    run = topic_store.build_run_record(
        artifacts,
        artifact_sha256="18d57e3071f046527570e4ee6667426e76df58fc0c29557bb8a03d67b87d8ebc",
    )

    assert run.created_at == "2026-07-02 16:01:09"
    assert run.input_fingerprint == "ecb7c06943b7d44fffde4e8761f281546237148d1f10ed088017d96e584fb135"


def test_validate_stage_schema_rejects_non_isolated_schema() -> None:
    """Given a non-stage schema, When schema is validated, Then writes are refused."""
    with pytest.raises(topic_store.TopicStoreError):
        topic_store.validated_stage_schema("prod_mart")


def test_run_table_ddl_and_upsert_include_input_fingerprint() -> None:
    """Given topic run storage SQL, When generated, Then the fingerprint column is carried."""
    _, runs_ddl = topic_store_db.topic_table_ddl()
    upsert = topic_store_db._run_upsert_sql(topic_store_db.SCHEMA)

    assert "input_fingerprint CHAR(64)" in runs_ddl
    assert "input_fingerprint" in upsert
    assert "VALUES (schema_stage_hash)" not in upsert


def test_topic_store_target_pair_defaults_to_live_tables(monkeypatch) -> None:
    monkeypatch.delenv("BRAND_ACTIVITY_TOPICS_TARGET_TABLE", raising=False)
    monkeypatch.delenv("BRAND_ACTIVITY_TOPIC_RUNS_TARGET_TABLE", raising=False)

    assert topic_store_db.resolve_topic_tables() == topic_store_db.TopicTables(
        topics=topic_store_db.TOPICS_TABLE,
        runs=topic_store_db.RUNS_TABLE,
    )


def test_topic_store_target_pair_accepts_only_the_staging_pair(monkeypatch) -> None:
    monkeypatch.setenv(
        "BRAND_ACTIVITY_TOPICS_TARGET_TABLE",
        topic_store_db.STAGING_TOPICS_TABLE,
    )
    monkeypatch.setenv(
        "BRAND_ACTIVITY_TOPIC_RUNS_TARGET_TABLE",
        topic_store_db.STAGING_RUNS_TABLE,
    )

    assert topic_store_db.resolve_topic_tables() == topic_store_db.TopicTables(
        topics=topic_store_db.STAGING_TOPICS_TABLE,
        runs=topic_store_db.STAGING_RUNS_TABLE,
    )


def test_topic_store_target_pair_rejects_mixed_live_and_staging(monkeypatch) -> None:
    monkeypatch.setenv("BRAND_ACTIVITY_TOPICS_TARGET_TABLE", topic_store_db.STAGING_TOPICS_TABLE)
    monkeypatch.setenv("BRAND_ACTIVITY_TOPIC_RUNS_TARGET_TABLE", topic_store_db.RUNS_TABLE)

    with pytest.raises(topic_store.TopicStoreError, match="approved live or staging pair"):
        topic_store_db.resolve_topic_tables()


def test_store_summary_validation_rejects_zero_row_save() -> None:
    """Given built records, When persistence evidence is zero, Then the save must fail loudly."""
    summary = topic_store_db.StoreSummary(
        run_id="brand_activity_replay_20260703_000739",
        topic_record_count=11,
        topic_brand_count=115,
        stored_topic_rows=0,
        stored_run_rows=0,
    )

    with pytest.raises(topic_store.TopicStoreError, match="zero-row DB save"):
        topic_store_db.ensure_store_summary_nonzero(summary)


def test_store_summary_validation_accepts_persisted_rows() -> None:
    """Given stored rows matching a measured run, When validated, Then no error is raised."""
    summary = topic_store_db.StoreSummary(
        run_id="brand_activity_replay_20260703_000739",
        topic_record_count=11,
        topic_brand_count=115,
        stored_topic_rows=11,
        stored_run_rows=1,
    )

    topic_store_db.ensure_store_summary_nonzero(summary)


def test_upsert_topic_results_recovers_zero_topic_count_on_bounded_retry() -> None:
    """Given a transient zero topic readback, When storing records, Then the retry evidence is used."""
    artifacts = _artifact_payload()
    run = topic_store.build_run_record(artifacts, artifact_sha256="sha")
    records = topic_store.build_topic_records(artifacts)
    connection = _StoreConnection(
        {
            topic_store_db.RUNS_TABLE: [1],
            topic_store_db.TOPICS_TABLE: [0, len(records)],
        }
    )

    summary = topic_store_db.upsert_topic_results(
        connection,
        schema=topic_store_db.SCHEMA,
        run=run,
        records=records,
    )

    assert summary.stored_topic_rows == len(records)
    assert summary.stored_run_rows == 1
    assert summary.count_retry_used is True
    assert connection.count_queries[topic_store_db.TOPICS_TABLE] == 2
    topic_store_db.ensure_store_summary_nonzero(summary)


def test_upsert_topic_results_keeps_zero_after_bounded_retry_for_real_failure() -> None:
    """Given repeated zero topic readbacks, When storing records, Then validation still fails loudly."""
    artifacts = _artifact_payload()
    run = topic_store.build_run_record(artifacts, artifact_sha256="sha")
    records = topic_store.build_topic_records(artifacts)
    connection = _StoreConnection(
        {
            topic_store_db.RUNS_TABLE: [1],
            topic_store_db.TOPICS_TABLE: [0, 0, 0],
        }
    )

    summary = topic_store_db.upsert_topic_results(
        connection,
        schema=topic_store_db.SCHEMA,
        run=run,
        records=records,
    )

    assert summary.stored_topic_rows == 0
    assert summary.count_retry_used is False
    assert connection.count_queries[topic_store_db.TOPICS_TABLE] == 3
    with pytest.raises(topic_store.TopicStoreError, match="zero-row DB save"):
        topic_store_db.ensure_store_summary_nonzero(summary)


def test_upsert_topic_results_does_not_retry_nonzero_first_readback() -> None:
    """Given nonzero readback evidence, When storing records, Then no defensive retry is used."""
    artifacts = _artifact_payload()
    run = topic_store.build_run_record(artifacts, artifact_sha256="sha")
    records = topic_store.build_topic_records(artifacts)
    connection = _StoreConnection(
        {
            topic_store_db.RUNS_TABLE: [1],
            topic_store_db.TOPICS_TABLE: [len(records)],
        }
    )

    summary = topic_store_db.upsert_topic_results(
        connection,
        schema=topic_store_db.SCHEMA,
        run=run,
        records=records,
    )

    assert summary.stored_topic_rows == len(records)
    assert summary.count_retry_used is False
    assert connection.count_queries[topic_store_db.TOPICS_TABLE] == 1


def test_load_artifacts_requires_existing_audit_files(tmp_path: Path) -> None:
    """Given an incomplete audit directory, When loading artifacts, Then the missing file is explicit."""
    with pytest.raises(topic_store.TopicStoreError, match="run_summary.json"):
        topic_store.load_artifacts(tmp_path)


def test_build_verification_uses_measured_call_log_tokens() -> None:
    """Given current audit files, When verification is built, Then measured route and token totals survive."""
    run_summary = {
        "tag": "brand_activity_replay_20260702_160109",
        "executed_call_count": 2,
        "raw_text_leak_count": 0,
        "quality_grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0},
        "complex_label_count": 0,
        "brand_specific_duplicate_pair_count": 0,
    }
    call_log = [
        {
            "status": "ok",
            "backend": "direct_serving",
            "endpoint": "http://llmops-gateway-api-service:8080/rep/serving/163/chat/completions",
            "model_id": "genos-flash",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "retry": {"retry_count": 1},
        },
        {
            "status": "ok",
            "backend": "direct_serving",
            "endpoint": "http://llmops-gateway-api-service:8080/rep/serving/163/chat/completions",
            "model_id": "genos-flash",
            "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            "retry": {"retry_count": 0},
        },
    ]

    payload = verification.build_verification(
        run_summary=run_summary,
        call_log=call_log,
        quality_summary={"grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0}},
        label_quality_summary={"complex_label_count": 0, "brand_specific_duplicate_pair_count": 0},
    )

    assert payload["executed_call_count"] == 2
    assert payload["prompt_tokens"] == 30
    assert payload["completion_tokens"] == 5
    assert payload["estimated_usd_vertex_flash_proxy"] == 0.0
    assert payload["backend_counts"] == {"direct_serving": 2}
    assert payload["model_counts"] == {"genos-flash": 2}
    assert payload["status_counts"] == {"ok": 2}
    assert payload["retry_count"] == 1
    assert payload["serving_route"] == {
        "backend": "direct_serving",
        "gateway_used": True,
        "manual_hosts_used": False,
        "model_id": "genos-flash",
    }


def test_derive_verification_file_unblocks_artifact_loading(tmp_path: Path) -> None:
    """Given a current audit dir without legacy verification, When derived, Then store loading works."""
    write_json(
        tmp_path / "run_summary.json",
        {
            "tag": "brand_activity_replay_20260702_160109",
            "executed_call_count": 1,
            "raw_text_leak_count": 0,
            "quality_grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0},
        },
    )
    write_json(
        tmp_path / "call_log_sanitized.json",
        [
            {
                "status": "ok",
                "backend": "direct_serving",
                "model_id": "genos-flash",
                "serving_id": "163",
                "usage": {"prompt_tokens": 100, "completion_tokens": 25},
                "retry": {"retry_count": 0},
            }
        ],
    )
    write_json(tmp_path / "quality_summary.json", {"grade_distribution": {"A": 1, "B": 0, "C": 0, "D": 0}})
    write_json(tmp_path / "label_quality_summary.json", {"complex_label_count": 0, "brand_specific_duplicate_pair_count": 0})
    write_json(tmp_path / "brand_results_sanitized.json", {})
    write_json(
        tmp_path / "viz_payload.json",
        {
            "markets": [
                {
                    "scope_id": "atc4:A02B2",
                    "scope_key": "A02B2",
                    "display_name": "PPI Market",
                    "atc4_values": ["A02B2"],
                    "quality_grade": "A",
                    "axis_row_count": 10,
                }
            ],
            "brand_results": [],
        },
    )
    write_json(tmp_path / "axis_results_sanitized.json", {"A02B2": {"scope_id": "atc4:A02B2", "source_row_count": 10, "topics": []}})

    verification.write_verification_file(tmp_path, derived_post_hoc=True)
    artifacts = topic_store.load_artifacts(tmp_path)
    run = topic_store.build_run_record(artifacts, artifact_sha256="18d57e3071f046527570e4ee6667426e76df58fc0c29557bb8a03d67b87d8ebc")

    assert artifacts.verification["derived_post_hoc"] is True
    assert run.total_prompt_tokens == 100
    assert run.total_completion_tokens == 25
    assert run.route == "direct_serving"


class _StoreConnection:
    def __init__(self, count_rows: dict[str, list[int]]) -> None:
        self._count_rows = {table: list(values) for table, values in count_rows.items()}
        self.count_queries = {table: 0 for table in count_rows}
        self.commits = 0

    def cursor(self) -> "_StoreCursor":
        return _StoreCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def next_count(self, table: str) -> int:
        self.count_queries[table] += 1
        values = self._count_rows[table]
        if len(values) > 1:
            return values.pop(0)
        return values[0]


class _StoreCursor:
    def __init__(self, connection: _StoreConnection) -> None:
        self._connection = connection
        self._last_row = {"row_count": 0}

    def __enter__(self) -> "_StoreCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        if "SELECT COUNT(*)" not in sql:
            return
        table = _count_table(sql)
        self._last_row = {"row_count": self._connection.next_count(table)}

    def executemany(self, sql: str, values: list[tuple[object, ...]]) -> None:
        return None

    def fetchone(self) -> dict[str, int]:
        return self._last_row


def _count_table(sql: str) -> str:
    for table in (topic_store_db.RUNS_TABLE, topic_store_db.TOPICS_TABLE):
        if f"`.`{table}`" in sql:
            return table
    raise AssertionError(f"unexpected count SQL: {sql}")
