from __future__ import annotations

from datetime import datetime

from agent2_variant_repair import build_fallback_record, repair_variant_sql
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


def test_repair_sql_updates_only_requested_variant_and_lineage() -> None:
    sql = repair_variant_sql("candidate_table", "long")

    assert "ai_analysis_long_json = %s" in sql
    assert "long_generation_status = %s" in sql
    assert "ai_analysis_short_json" not in sql
    assert "WHERE brand_key = %s" in sql
