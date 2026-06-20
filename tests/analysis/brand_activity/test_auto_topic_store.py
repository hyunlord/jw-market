from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import topic_store


def _artifact_payload() -> topic_store.TopicArtifacts:
    """Create a tiny measured payload shaped like the latest top-7 run."""
    return topic_store.TopicArtifacts(
        run_summary={
            "tag": "serving_direct_singleconcept_top7_exec_20260620_143124",
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
                    "topic_shares": [{"topic_id": "T1", "label": "효능", "share_pct": 91.0}],
                    "brand_specific_topics": [{"topic_id": "B1", "label": "보험", "share_pct": 4.0}],
                    "etc_pct": 5.0,
                },
                {
                    "sample_key": "A02B2:RABEKHAN",
                    "scope_id": "atc4:A02B2",
                    "scope_key": "A02B2",
                    "brand": "RABEKHAN",
                    "atc4": "A02B2",
                    "row_count": 20,
                    "topic_shares": [{"topic_id": "T1", "label": "효능", "share_pct": 100.0}],
                    "brand_specific_topics": [],
                    "etc_pct": 0.0,
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


def test_validate_stage_schema_rejects_non_isolated_schema() -> None:
    """Given a non-stage schema, When schema is validated, Then writes are refused."""
    with pytest.raises(topic_store.TopicStoreError):
        topic_store.validated_stage_schema("prod_mart")


def test_load_artifacts_requires_existing_audit_files(tmp_path: Path) -> None:
    """Given an incomplete audit directory, When loading artifacts, Then the missing file is explicit."""
    with pytest.raises(topic_store.TopicStoreError, match="run_summary.json"):
        topic_store.load_artifacts(tmp_path)
