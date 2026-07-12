from __future__ import annotations

from datetime import datetime

from agent2_variant_candidate_builder import _lineage
from agent2_variant_contract import VariantLineage


def test_deterministic_complete_lineage_does_not_invent_workflow_binding() -> None:
    run = {
        "run_id": 7,
        "bundle_hash": "sha256:" + "a" * 64,
        "created_at": datetime(2026, 7, 12, 1, 2, 3),
        "snapshot_at": datetime(2026, 7, 10, 4, 5, 6),
    }

    value = _lineage(run, deterministic=True)
    lineage = VariantLineage(**value)

    assert lineage.generation_status == "complete"
    assert lineage.workflow_id is None
    assert lineage.workflow_revision_id is None
    assert lineage.input_hash == "a" * 64
    assert lineage.source_epoch == "2026-07-10T04:05:06"


def test_llm_complete_lineage_binds_wf217_revision_3727() -> None:
    run = {
        "run_id": 8,
        "bundle_hash": "b" * 64,
        "created_at": datetime(2026, 7, 12, 1, 2, 3),
        "snapshot_at": datetime(2026, 7, 10, 4, 5, 6),
    }

    value = _lineage(run, deterministic=False)
    lineage = VariantLineage(**value)

    assert lineage.workflow_id == 217
    assert lineage.workflow_revision_id == 3727
