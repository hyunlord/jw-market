from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.etl.io.catalog import db_sync
from pipeline.etl.mi_master_definition_refresh import (
    AffectedDefinition,
    CatalogTableSnapshot,
    CandidateSeedContract,
    DefinitionApprovalIdentity,
    MiMasterRefreshCandidate,
    RefreshCorpus,
    RefreshPublishPlan,
    ReferenceReport,
    RemovedIdApproval,
    ReplacementReferencePolicy,
    StrategicMarketValidationInput,
    atomic_publish_candidate,
    build_catalog_diff_hash,
    build_replacement_diff,
    build_replacement_parity,
    load_candidate_seed,
    plan_affected_scope,
    validate_candidate_seed,
    validate_candidate_seed_contract,
    validate_definition_approval,
    validate_manifest_equality,
    validate_replacement_diff,
    validate_replacement_parity,
    validate_removed_id_references,
    validate_refresh_publish_plan,
    validate_strategic_market_refresh,
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

    # When / Then: exact identity validates, while cache-cause/deep-analysis are rejected.
    validate_candidate_seed(candidate)
    validate_definition_approval(
        candidate,
        {
            "approved": True,
            "mi_master_sha256": "c" * 64,
            "catalog_diff_hash": "d" * 64,
            "run_id": "mi-refresh-20260806",
        },
        expected=DefinitionApprovalIdentity(
            mi_master_sha256="c" * 64,
            catalog_diff_hash="d" * 64,
            run_id="mi-refresh-20260806",
        ),
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
    backup_corpus = tmp_path / "backup-corpus"
    journal = tmp_path / "journal.jsonl"
    live.mkdir()
    candidate.mkdir()
    backup_corpus.mkdir()
    (live / "manifest.json").write_text('{"version": 1}', encoding="utf-8")
    (candidate / "manifest.json").write_text('{"version": 2}', encoding="utf-8")
    (backup_corpus / "manifest.json").write_text('{"version": 1}', encoding="utf-8")
    journal.write_text("", encoding="utf-8")

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
            corpus=RefreshCorpus(candidate, backup_corpus),
            approval_identity=DefinitionApprovalIdentity(
                mi_master_sha256="e" * 64,
                catalog_diff_hash="f" * 64,
                run_id="mi-refresh-20260806",
            ),
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


def test_catalog_diff_hash_is_deterministic_and_drives_approval_identity() -> None:
    # Given: the same prior/new catalog diff presented in different insertion orders.
    prior_a = {
        "catalog_cd_market": {"cd_001": "h1"},
        "catalog_ml_market": {"ml_001": "h1", "ml_002": "h2"},
    }
    new_a = {
        "catalog_ml_market": {"ml_001": "h1", "ml_003": "h3"},
        "catalog_cd_market": {"cd_001": "h1"},
    }
    prior_b = {
        "catalog_ml_market": {"ml_002": "h2", "ml_001": "h1"},
        "catalog_cd_market": {"cd_001": "h1"},
    }
    new_b = {
        "catalog_cd_market": {"cd_001": "h1"},
        "catalog_ml_market": {"ml_003": "h3", "ml_001": "h1"},
    }

    # When: a catalog diff hash is computed.
    digest = build_catalog_diff_hash(prior_a, new_a)

    # Then: order does not matter and approval identity has only the three fields.
    assert digest == build_catalog_diff_hash(prior_b, new_b)
    assert DefinitionApprovalIdentity(
        mi_master_sha256="a" * 64,
        catalog_diff_hash=digest,
        run_id="run-1",
    ).as_dict() == {
        "mi_master_sha256": "a" * 64,
        "catalog_diff_hash": digest,
        "run_id": "run-1",
    }


def test_replacement_parity_exposes_counts_ids_hashes_and_rejects_mismatch() -> None:
    # Given: candidate rows add one ML, remove one ML, and change one shared ML.
    parity = build_replacement_parity(
        before_by_table={"catalog_ml_market": {"ml_001": "old", "ml_002": "same"}},
        after_by_table={"catalog_ml_market": {"ml_002": "same", "ml_003": "new"}},
        before_parquet_hashes={"catalog_ml_market": "a" * 64},
        after_parquet_hashes={"catalog_ml_market": "b" * 64},
        expected_after_counts={"catalog_ml_market": 2},
    )

    # When / Then: parity exposes all comparison dimensions.
    row = parity[0]
    assert row.row_count_before == 2
    assert row.row_count_after == 2
    assert row.row_count_expected == 2
    assert row.removed_ids == ("ml_001",)
    assert row.added_ids == ("ml_003",)
    assert row.changed_ids == ()
    assert row.before_parquet_sha256 == "a" * 64
    assert row.after_parquet_sha256 == "b" * 64
    validate_replacement_parity(parity)

    bad = build_replacement_parity(
        before_by_table={"catalog_ml_market": {"ml_001": "old"}},
        after_by_table={"catalog_ml_market": {"ml_001": "new", "ml_002": "new"}},
        before_parquet_hashes={"catalog_ml_market": "a" * 64},
        after_parquet_hashes={"catalog_ml_market": "short"},
        expected_after_counts={"catalog_ml_market": 1},
    )
    with pytest.raises(ValueError, match="catalog_ml_market"):
        validate_replacement_parity(bad)


def test_removed_ids_with_references_require_inactive_decision() -> None:
    # Given: an ID slated for removal is still referenced downstream.
    report = ReferenceReport(
        mart_references={"ml_001": ("mart_strategic_ml_brand_metric",)},
        cache_references={"ml_001": ("cache_brands",)},
        saved_filter_references={"ml_001": ("filter-7",)},
        inactive_decisions=(),
    )

    # When / Then: referenced removals cannot be deleted without inactive decision.
    with pytest.raises(ValueError, match="inactive decision"):
        validate_removed_id_references(("ml_001",), report)

    validate_removed_id_references(
        ("ml_001",),
        ReferenceReport(
            mart_references=report.mart_references,
            cache_references=report.cache_references,
            saved_filter_references=report.saved_filter_references,
            inactive_decisions=("ml_001",),
        ),
    )


def test_affected_scope_splits_ml_cd_and_unchanged_ids() -> None:
    # Given: ML and CD diffs plus existing general ATC4 coverage.
    plan = plan_affected_scope(
        affected_definitions=(
            AffectedDefinition(
                market_id="ml_002",
                atc4_codes=("C10A1", "Z99A1"),
                cache_tables=("cache_brands",),
                cd_ids=("cd_002",),
            ),
        ),
        existing_general_atc4=("C10A1",),
        all_ml_ids=("ml_001", "ml_002"),
        all_cd_ids=("cd_001", "cd_002"),
    )

    # Then: affected and unchanged identities are split; existing ATC4 is skipped.
    assert plan.affected_ml_ids == ("ml_002",)
    assert plan.affected_cd_ids == ("cd_002",)
    assert plan.unchanged_ml_ids == ("ml_001",)
    assert plan.unchanged_cd_ids == ("cd_001",)
    assert plan.general_rebuild_atc4 == ("Z99A1",)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("unchanged_hash", "unchanged market hash changed"),
        ("cd_not_subset", "CD membership is not a subset"),
        ("sigma", "sigma mismatch"),
    ],
)
def test_validation_gate_fails_on_required_refresh_violations(
    case: str,
    expected: str,
) -> None:
    # Given: a normal validation payload with one unchanged ML and subset CD.
    payload = StrategicMarketValidationInput(
        unchanged_market_hash_before={"ml_001": "h1"},
        unchanged_market_hash_after={"ml_001": "h1"},
        ml_members={"ml_002": ("brand_a", "brand_b")},
        cd_members={"cd_002": ("brand_a",)},
        cd_parent_ml={"cd_002": "ml_002"},
        sigma_before={"ml_002": 100},
        sigma_after={"ml_002": 100},
    )
    if case == "unchanged_hash":
        payload = payload.with_overrides(unchanged_market_hash_after={"ml_001": "h2"})
    if case == "cd_not_subset":
        payload = payload.with_overrides(cd_members={"cd_002": ("brand_c",)})
    if case == "sigma":
        payload = payload.with_overrides(sigma_after={"ml_002": 99})

    # When / Then: the gate fails closed with the named reason.
    with pytest.raises(ValueError, match=expected):
        validate_strategic_market_refresh(payload)


def test_validation_gate_passes_normal_refresh() -> None:
    validate_strategic_market_refresh(
        StrategicMarketValidationInput(
            unchanged_market_hash_before={"ml_001": "h1"},
            unchanged_market_hash_after={"ml_001": "h1"},
            ml_members={"ml_002": ("brand_a", "brand_b")},
            cd_members={"cd_002": ("brand_a",)},
            cd_parent_ml={"cd_002": "ml_002"},
            sigma_before={"ml_002": 100},
            sigma_after={"ml_002": 100},
        )
    )


def test_candidate_seed_contract_covers_live_catalog_and_four_strategic_tables() -> None:
    # Given: a seed contract with live catalog plus four strategic mart tables.
    contract = CandidateSeedContract(
        live_catalog=(
            CatalogTableSnapshot("catalog_ml_market", 2, "a" * 64),
            CatalogTableSnapshot("catalog_cd_market", 2, "b" * 64),
            CatalogTableSnapshot("catalog_strategic_brand", 20, "c" * 64),
        ),
        strategic_tables=(
            CatalogTableSnapshot("mart_strategic_ml_brand_metric", 10, "d" * 64),
            CatalogTableSnapshot("mart_strategic_ml_market_metric", 2, "e" * 64),
            CatalogTableSnapshot("mart_strategic_cd_brand_metric", 8, "f" * 64),
            CatalogTableSnapshot("mart_strategic_cd_market_metric", 2, "1" * 64),
        ),
    )

    # When / Then: the full contract passes, but missing one strategic table fails.
    validate_candidate_seed_contract(contract)
    with pytest.raises(ValueError, match="mart_strategic_cd_market_metric"):
        validate_candidate_seed_contract(
            CandidateSeedContract(
                live_catalog=contract.live_catalog,
                strategic_tables=contract.strategic_tables[:3],
            )
        )


def test_publish_plan_requires_corpora_journal_and_approval_identity(
    tmp_path: Path,
) -> None:
    # Given: candidate and backup corpora plus a journal and exact approval identity.
    candidate_dir = tmp_path / "candidate"
    backup_dir = tmp_path / "backup"
    live_dir = tmp_path / "live"
    journal = tmp_path / "journal.jsonl"
    for directory in (candidate_dir, backup_dir, live_dir):
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    journal.write_text("", encoding="utf-8")
    identity = DefinitionApprovalIdentity(
        mi_master_sha256="a" * 64,
        catalog_diff_hash="b" * 64,
        run_id="run-1",
    )
    plan = RefreshPublishPlan(
        candidate=MiMasterRefreshCandidate(
            candidate_id="run-1",
            mi_master_sha256="a" * 64,
            manifest_sha256="b" * 64,
            allowed_cache_tables=("cache_brands", "cache_market_status"),
        ),
        candidate_dir=candidate_dir,
        live_dir=live_dir,
        backup_dir=backup_dir,
        journal_path=journal,
        corpus=RefreshCorpus(candidate_dir, backup_dir),
        approval_identity=identity,
    )

    # When / Then: complete preconditions pass; missing approval identity fails.
    validate_refresh_publish_plan(plan)
    with pytest.raises(ValueError, match="approval identity"):
        validate_refresh_publish_plan(
            RefreshPublishPlan(
                candidate=plan.candidate,
                candidate_dir=candidate_dir,
                live_dir=live_dir,
                backup_dir=backup_dir,
                journal_path=journal,
                corpus=RefreshCorpus(candidate_dir, backup_dir),
                approval_identity=None,
            )
        )


def test_publish_plan_rejects_approval_identity_not_bound_to_candidate(
    tmp_path: Path,
) -> None:
    # Given: a publish plan whose approval names the wrong candidate identity.
    candidate_dir = tmp_path / "candidate"
    backup_dir = tmp_path / "backup"
    live_dir = tmp_path / "live"
    journal = tmp_path / "journal.jsonl"
    for directory in (candidate_dir, backup_dir, live_dir):
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    journal.write_text("", encoding="utf-8")
    candidate = MiMasterRefreshCandidate(
        candidate_id="run-1",
        mi_master_sha256="a" * 64,
        manifest_sha256="b" * 64,
        allowed_cache_tables=("cache_brands", "cache_market_status"),
    )

    # When / Then: catalog diff and run identity must both bind to the candidate.
    with pytest.raises(ValueError, match="catalog_diff_hash"):
        validate_refresh_publish_plan(
            RefreshPublishPlan(
                candidate=candidate,
                candidate_dir=candidate_dir,
                live_dir=live_dir,
                backup_dir=backup_dir,
                journal_path=journal,
                corpus=RefreshCorpus(candidate_dir, backup_dir),
                approval_identity=DefinitionApprovalIdentity(
                    mi_master_sha256="a" * 64,
                    catalog_diff_hash="c" * 64,
                    run_id="run-1",
                ),
            )
        )
    with pytest.raises(ValueError, match="run_id"):
        validate_refresh_publish_plan(
            RefreshPublishPlan(
                candidate=candidate,
                candidate_dir=candidate_dir,
                live_dir=live_dir,
                backup_dir=backup_dir,
                journal_path=journal,
                corpus=RefreshCorpus(candidate_dir, backup_dir),
                approval_identity=DefinitionApprovalIdentity(
                    mi_master_sha256="a" * 64,
                    catalog_diff_hash="b" * 64,
                    run_id="run-2",
                ),
            )
        )


def test_definition_approval_rejects_identity_not_bound_to_candidate() -> None:
    # Given: payload and expected identity agree, but that identity is not this candidate.
    candidate = MiMasterRefreshCandidate(
        candidate_id="run-1",
        mi_master_sha256="a" * 64,
        manifest_sha256="b" * 64,
        allowed_cache_tables=("cache_brands", "cache_market_status"),
    )

    # When / Then: validation fails on candidate binding, not just payload equality.
    with pytest.raises(ValueError, match="catalog_diff_hash"):
        validate_definition_approval(
            candidate,
            {
                "approved": True,
                "mi_master_sha256": "a" * 64,
                "catalog_diff_hash": "c" * 64,
                "run_id": "run-1",
            },
            expected=DefinitionApprovalIdentity(
                mi_master_sha256="a" * 64,
                catalog_diff_hash="c" * 64,
                run_id="run-1",
            ),
        )
    with pytest.raises(ValueError, match="run_id"):
        validate_definition_approval(
            candidate,
            {
                "approved": True,
                "mi_master_sha256": "a" * 64,
                "catalog_diff_hash": "b" * 64,
                "run_id": "run-2",
            },
            expected=DefinitionApprovalIdentity(
                mi_master_sha256="a" * 64,
                catalog_diff_hash="b" * 64,
                run_id="run-2",
            ),
        )
