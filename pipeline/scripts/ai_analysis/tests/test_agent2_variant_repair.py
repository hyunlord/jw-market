from __future__ import annotations

from datetime import datetime

from agent2_variant_repair import build_fallback_record, load_validated_trace_records, repair_variant_sql
from bundle_builder.agent2_zero_template import KpiSnapshot


def test_fallback_record_uses_actual_snapshot_and_truthful_status() -> None:
    snapshot = KpiSnapshot(
        brand="브랜드",
        market_name="시장",
        rank=2,
        share_pct=12.5,
        cagr_pct=3.2,
        hhi=1234.5,
        market_size_recent=50_000_000_000,
    )

    record = build_fallback_record(
        brand_key="브랜드",
        variant="short",
        snapshot=snapshot,
        source_epoch="2026-Q1",
        generated_at=datetime(2026, 7, 12, 10, 0, 0),
    )

    assert record.payload["analysis_variant"] == "short"
    assert record.payload["model_version"] == "deterministic-template-fallback"
    assert "2위" in record.payload["phenomenon"]["body"]
    assert record.lineage.generation_status == "complete_template_fallback"
    assert record.lineage.deterministic is True
    assert record.lineage.workflow_id is None
    assert record.lineage.source_epoch == "2026-Q1"


def test_repair_sql_upserts_only_requested_variant_and_lineage() -> None:
    sql = repair_variant_sql("candidate_table", "long")

    assert sql.startswith("INSERT INTO candidate_table (brand, brand_key, ai_analysis_long_json")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "ai_analysis_long_json = VALUES(ai_analysis_long_json)" in sql
    assert "long_generation_status = VALUES(long_generation_status)" in sql
    assert "ai_analysis_short_json" not in sql


def test_validated_trace_loader_keeps_variants_separate(monkeypatch) -> None:
    runs = {
        "short": {"브랜드": {"run_id": 11}},
        "long": {"브랜드": {"run_id": 12}},
    }
    monkeypatch.setattr(
        "agent2_variant_repair._select_runs",
        lambda conn, names, variant, zero: runs[variant],
    )
    monkeypatch.setattr(
        "agent2_variant_repair._load_outputs",
        lambda conn, run_ids: {run_ids[0]: []},
    )
    monkeypatch.setattr(
        "agent2_variant_repair._load_payload",
        lambda run, outputs, variant: {"analysis_variant": variant},
    )
    monkeypatch.setattr(
        "agent2_variant_repair._lineage",
        lambda run, deterministic: {
            "workflow_id": 217,
            "workflow_revision_id": 3727,
            "generation_id": f"zeta-run-{run['run_id']}",
            "input_hash": "a" * 64,
            "generated_at": datetime(2026, 7, 12, 10, 0, 0),
            "source_epoch": "2026-07",
            "generation_status": "complete",
            "deterministic": False,
        },
    )

    records = load_validated_trace_records(
        object(),
        [("브랜드", "브랜드", "short"), ("브랜드", "브랜드", "long")],
    )

    assert records[("브랜드", "short")].payload["analysis_variant"] == "short"
    assert records[("브랜드", "long")].payload["analysis_variant"] == "long"
