from __future__ import annotations

from datetime import datetime

import pytest

from agent2_variant_contract import VariantContractError, VariantLineage, parse_legacy_lineage
from agent2_variant_promotion import (
    PromotionRow,
    VariantRecord,
    additive_schema_sql,
    assert_completion,
    atomic_swap_sql,
    promotion_insert_sql,
    promotion_values,
    load_rows,
    should_skip,
)


def _payload(variant: str) -> dict:
    payload = {"analysis_variant": variant}
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        payload[stage] = {"title": stage, "body": f"{stage} body", "bullets": []}
    return payload


def _record(variant: str, input_hash: str) -> VariantRecord:
    return VariantRecord(
        payload=_payload(variant),
        lineage=VariantLineage(217, 3727, f"gen-{variant}", input_hash, datetime(2026, 7, 12), "2026-07-12", "complete"),
    )


def test_incomplete_payload_fails_closed_before_insert():
    row = PromotionRow("브랜드", "브랜드", None, _record("short", "a" * 64), _record("long", "b" * 64))
    row.short.payload["cause"]["body"] = ""

    with pytest.raises(VariantContractError, match="cause.body"):
        promotion_values(row)


def test_payload_and_variant_lineage_share_one_insert_statement():
    row = PromotionRow("브랜드", "브랜드", "ml_001", _record("short", "a" * 64), _record("long", "b" * 64))

    sql = promotion_insert_sql("candidate_table")
    values = promotion_values(row)

    assert "ai_analysis_short_json" in sql
    assert "short_input_hash" in sql
    assert "ai_analysis_long_json" in sql
    assert "long_generation_status" in sql
    assert len(values) == sql.count("%s")


def test_same_complete_input_hash_is_idempotently_skipped():
    incoming = _record("short", "a" * 64)
    assert should_skip("a" * 64, "complete", incoming)
    assert not should_skip("a" * 64, "legacy_unbound", incoming)
    assert not should_skip("b" * 64, "complete", incoming)


def test_short_and_long_lineage_are_kept_separate():
    row = PromotionRow("브랜드", "브랜드", None, _record("short", "a" * 64), _record("long", "b" * 64))
    values = promotion_values(row)
    assert values[8] == "a" * 64
    assert values[15] == "b" * 64


def test_legacy_backfill_does_not_invent_unrecoverable_lineage():
    lineage = parse_legacy_lineage('{"generated_at":"2026-07-01T03:04:05","run_id_phase_zeta":12}')
    assert lineage is not None
    assert lineage.generation_id == "zeta-run-12"
    assert lineage.generated_at == datetime(2026, 7, 1, 3, 4, 5)
    assert lineage.workflow_id is None
    assert lineage.workflow_revision_id is None
    assert lineage.input_hash is None
    assert lineage.source_epoch is None
    assert lineage.generation_status == "legacy_unbound"


def test_additive_schema_only_emits_missing_columns():
    sql = additive_schema_sql({"brand", "brand_key", "short_workflow_id"})
    assert all(statement.startswith("ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN") for statement in sql)
    assert all("brand_key" not in statement for statement in sql)
    assert all("short_workflow_id" not in statement for statement in sql)
    assert any("long_generation_status" in statement for statement in sql)


def test_completion_gate_rejects_partial_population():
    with pytest.raises(RuntimeError, match="completion gate failed"):
        assert_completion({"route_count": 24789, "short_complete": 24788, "long_complete": 24789, "inserted": 24789}, 24789)
    assert_completion({"route_count": 24789, "short_complete": 24789, "long_complete": 24789, "inserted": 24789}, 24789)


def test_atomic_swap_is_single_rename_statement():
    assert atomic_swap_sql("live", "candidate", "backup") == "RENAME TABLE live TO backup, candidate TO live"


def test_jsonl_loader_rejects_incomplete_complete_lineage(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text(
        '{"brand":"b","brand_key":"b","short":{"payload":{},"lineage":{"generation_status":"complete"}},'
        '"long":{"payload":{},"lineage":{"generation_status":"complete"}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid promotion row"):
        load_rows(path)


def test_template_fallback_is_complete_but_remains_distinguishable() -> None:
    lineage = VariantLineage(
        None,
        None,
        "template-fallback-brand-short",
        "c" * 64,
        datetime(2026, 7, 12),
        "2026-Q1",
        "complete_template_fallback",
        deterministic=True,
    )
    row = PromotionRow(
        "브랜드",
        "브랜드",
        None,
        VariantRecord(_payload("short"), lineage),
        _record("long", "d" * 64),
    )

    values = promotion_values(row)

    assert "complete_template_fallback" in values
    assert lineage.workflow_id is None
    assert lineage.workflow_revision_id is None


def test_template_fallback_requires_deterministic_lineage() -> None:
    with pytest.raises(VariantContractError, match="template fallback lineage must be deterministic"):
        VariantLineage(
            217,
            3727,
            "template-fallback-brand-short",
            "e" * 64,
            datetime(2026, 7, 12),
            "2026-Q1",
            "complete_template_fallback",
        )
