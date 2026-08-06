from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.etl.io.catalog import db_sync
from pipeline.etl.mi_master_definition_refresh import (
    AffectedDefinition,
    DefinitionApprovalIdentity,
    MiMasterRefreshCandidate,
    RefreshPublishPlan,
    RemovedIdApproval,
    ReplacementReferencePolicy,
    atomic_publish_candidate,
    build_replacement_diff,
    load_candidate_seed,
    plan_affected_scope,
    validate_candidate_seed,
    validate_definition_approval,
    validate_manifest_equality,
    validate_replacement_diff,
)
from pipeline.orchestrator.full_rehearsal_compare import CACHE_TABLES, OBSERVE_TABLES


def test_mi_master_sha256_is_carried_by_catalog_db_sync_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: catalog rows are loaded from a candidate generated from a specific MI Master.
    monkeypatch.setattr(
        db_sync,
        "_load_catalog_rows",
        lambda _root, spec, mi_master_sha256=None: (
            [
                {
                    "ml_id": "ml_001",
                    "source_file_version": "MI",
                    "mi_master_sha256": mi_master_sha256,
                }
            ]
            if spec.parquet_name == "ml_market"
            else [
                {
                    "cd_id": "cd_001",
                    "source_file_version": "MI",
                    "mi_master_sha256": mi_master_sha256,
                }
            ]
            if spec.parquet_name == "cd_market"
            else [
                {
                    "brand_id": "brand_001",
                    "source_file_version": "MI",
                    "mi_master_sha256": mi_master_sha256,
                }
            ],
            tmp_path / spec.parquet_name,
            "f" * 64,
        ),
    )

    # When: the sync runs in dry-run mode.
    results = db_sync.sync_catalog_tables(
        None,
        target_db="scratch",
        catalog_root=tmp_path,
        dry_run=True,
        mi_master_sha256="a" * 64,
    )

    # Then: each table result exposes the exact MI Master provenance.
    assert {result.mi_master_sha256 for result in results} == {"a" * 64}


def test_manifest_equality_gate_rejects_mixed_candidate_and_serving_hashes() -> None:
    # Given: the candidate and serving catalog were not produced from the same manifest.
    candidate = {"catalog_ml_market": "a" * 64, "catalog_cd_market": "a" * 64}
    serving = {"catalog_ml_market": "a" * 64, "catalog_cd_market": "b" * 64}

    # When / Then: equality is fail-closed by table.
    with pytest.raises(ValueError, match="catalog_cd_market"):
        validate_manifest_equality(candidate, serving)


def test_replacement_diff_requires_removed_id_approval_and_allows_additive_ids() -> None:
    # Given: a candidate removes one known ID and adds one new ID.
    diff = build_replacement_diff(
        reference_ids=("ml_001", "ml_002"),
        candidate_ids=("ml_002", "ml_003"),
    )

    # When / Then: removals fail unless the approval names exactly those IDs.
    assert diff.removed_ids == ("ml_001",)
    assert diff.added_ids == ("ml_003",)
    with pytest.raises(ValueError, match="removed IDs require approval"):
        validate_replacement_diff(
            diff,
            policy=ReplacementReferencePolicy.APPEND_OR_APPROVED_REMOVAL,
            removed_id_approval=None,
        )

    validate_replacement_diff(
        diff,
        policy=ReplacementReferencePolicy.APPEND_OR_APPROVED_REMOVAL,
        removed_id_approval=RemovedIdApproval(
            approved=True,
            removed_ids=("ml_001",),
            approver="pl@example.com",
            reason="MI Master definition retirement",
        ),
    )


def test_existing_general_atc4_definition_change_plans_zero_general_rebuilds() -> None:
    # Given: an affected definition references an ATC4 already present in general cache.
    definition = AffectedDefinition(
        market_id="ml_006",
        atc4_codes=("C10A1",),
        cache_tables=("cache_brands", "cache_market_status"),
    )

    # When: affected serving scope is planned.
    plan = plan_affected_scope(
        affected_definitions=(definition,),
        existing_general_atc4=("C10A1",),
    )

    # Then: portal caches are refreshed but general ATC4 rebuild count remains zero.
    assert plan.cache_tables == ("cache_brands", "cache_market_status")
    assert plan.general_rebuild_atc4 == ()
    assert plan.general_rebuild_count == 0


def test_candidate_seed_validation_and_definition_approval_identity_are_exact(
    tmp_path: Path,
) -> None:
    # Given: a candidate seed and an exact approval identity.
    seed_path = tmp_path / "candidate.json"
    seed_path.write_text(
        json.dumps(
            {
                "candidate_id": "mi-refresh-20260806",
                "mi_master_sha256": "c" * 64,
                "manifest_sha256": "d" * 64,
                "allowed_cache_tables": ["cache_brands", "cache_market_status"],
            }
        ),
        encoding="utf-8",
    )
    candidate = load_candidate_seed(seed_path)
    identity = DefinitionApprovalIdentity(
        candidate_id="mi-refresh-20260806",
        mi_master_sha256="c" * 64,
        manifest_sha256="d" * 64,
        approver="pl@example.com",
    )

    # When / Then: exact identity validates, while cache-cause/deep-analysis are rejected.
    validate_candidate_seed(candidate)
    validate_definition_approval(
        candidate,
        {
            "approved": True,
            "candidate_id": "mi-refresh-20260806",
            "mi_master_sha256": "c" * 64,
            "manifest_sha256": "d" * 64,
            "approver": "pl@example.com",
        },
        expected=identity,
    )

    with pytest.raises(ValueError, match="unsupported cache table"):
        validate_candidate_seed(
            MiMasterRefreshCandidate(
                candidate_id="bad",
                mi_master_sha256="c" * 64,
                manifest_sha256="d" * 64,
                allowed_cache_tables=("cache_cause",),
            )
        )


def test_atomic_publish_writes_journal_backup_and_candidate_manifest(
    tmp_path: Path,
) -> None:
    # Given: a staged candidate and an existing published manifest.
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    journal = tmp_path / "journal.jsonl"
    live.mkdir()
    candidate.mkdir()
    (live / "manifest.json").write_text('{"version": 1}', encoding="utf-8")
    (candidate / "manifest.json").write_text('{"version": 2}', encoding="utf-8")

    # When: the candidate is atomically published.
    result = atomic_publish_candidate(
        RefreshPublishPlan(
            candidate=MiMasterRefreshCandidate(
                candidate_id="mi-refresh-20260806",
                mi_master_sha256="e" * 64,
                manifest_sha256="f" * 64,
                allowed_cache_tables=("cache_brands", "cache_market_status"),
            ),
            candidate_dir=candidate,
            live_dir=live,
            backup_dir=tmp_path / "backup",
            journal_path=journal,
        )
    )

    # Then: the old live tree is backed up and the journal records the exact candidate.
    assert (live / "manifest.json").read_text(encoding="utf-8") == '{"version": 2}'
    assert (result.backup_dir / "manifest.json").read_text(encoding="utf-8") == '{"version": 1}'
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["backup_created", "candidate_published"]
    assert events[-1]["candidate_id"] == "mi-refresh-20260806"
    assert events[-1]["mi_master_sha256"] == "e" * 64


def test_refresh_boundary_never_promotes_cause_or_deep_analysis_caches() -> None:
    # Given / When / Then: the deterministic cache promotion boundary remains portal-cache only.
    assert CACHE_TABLES == ("cache_brands", "cache_market_status")
    observed = {table for table, family in OBSERVE_TABLES if family == "cache"}
    assert {"cache_cause", "cache_deep_analysis", "cache_deep_analysis_general"} <= observed


def test_mi_master_sha256_ddl_artifact_is_present_but_not_executed() -> None:
    # Given / When: DB change SQL is carried as an artifact only.
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "pipeline"
        / "etl"
        / "io"
        / "catalog"
        / "sql"
        / "mi_master_sha256_provenance.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")

    # Then: the artifact covers all catalog tables and contains no destructive DDL.
    assert "`catalog_ml_market`" in sql
    assert "`catalog_cd_market`" in sql
    assert "`catalog_strategic_brand`" in sql
    assert sql.count("ADD COLUMN `mi_master_sha256` CHAR(64) NULL") == 3
    assert "DROP " not in sql.upper()
