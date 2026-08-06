from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.job_runner import _StageTracker, expected_stages
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, STATUS_FAILED, open_sqlite_ledger
from pipeline.scripts.ingest_hook.mi_master_definition_commands import (
    PipelineCacheRefresher,
    PipelineCatalogSync,
    PipelinePrepareAdapters,
    PipelinePublisher,
    PipelineRuntimeCatalogInvalidator,
    PrepareFunctionBindings,
    publisher_from_request,
    run_candidate_build,
    run_post_gate,
    run_scope_plan,
    run_sigma,
    run_s2_catalog_sync,
)
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    CATEGORY,
    load_definition_request,
    main,
    prepare_definition_refresh_candidate,
)
from mi_master_definition_fixtures import (
    RecordingPrepareAdapters,
    approve,
    identity,
    prepare_request,
    workspace,
)
from ingest_fixtures import write_submission


SHA = "c" * 64


def _write_request(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _full_pipeline_payload(tmp_path: Path) -> dict[str, object]:
    request = prepare_request(tmp_path)
    payload = request.as_dict()
    payload.update(
        {
            "scope_plan": {
                "affected_definitions": [
                    {
                        "market_id": "ML17",
                        "atc4_codes": ["A10B"],
                        "cache_tables": ["cache_brands", "cache_market_status"],
                        "cd_ids": ["CD17"],
                    }
                ],
                "existing_general_atc4": [],
                "all_ml_ids": ["ML17", "ML18"],
                "all_cd_ids": ["CD17", "CD18"],
                "output_path": str(tmp_path / "plan.json"),
            },
            "candidate_build": {
                "target_db": "jw_mart_candidate",
                "source_db": "jw_mart_source",
                "general_source_db": "jw_mart_general",
                "catalog_root": str(tmp_path / "catalog"),
                "affected_ml_ids": ["ML17"],
                "affected_cd_ids": ["CD17"],
            },
            "validation": {
                "unchanged_market_hash_before": {"ML18": SHA},
                "unchanged_market_hash_after": {"ML18": SHA},
                "ml_members": {"ML17": ["BR1", "BR2"]},
                "cd_members": {"CD17": ["BR1"]},
                "cd_parent_ml": {"CD17": "ML17"},
                "sigma_before": {"ML18": 2},
                "sigma_after": {"ML18": 2},
            },
            "post_gate": {
                "candidate": {
                    "candidate_id": "cand-17",
                    "mi_master_sha256": request.identity.mi_master_sha256,
                    "manifest_sha256": request.identity.catalog_diff_hash,
                    "allowed_cache_tables": ["cache_brands", "cache_market_status"],
                },
                "seed_contract": {
                    "live_catalog": [
                        {"table_name": "catalog_ml_market", "row_count": 1, "sha256": SHA},
                        {"table_name": "catalog_cd_market", "row_count": 1, "sha256": SHA},
                        {
                            "table_name": "catalog_strategic_brand",
                            "row_count": 1,
                            "sha256": SHA,
                        },
                    ],
                    "strategic_tables": [
                        {
                            "table_name": "mart_strategic_ml_brand_metric",
                            "row_count": 1,
                            "sha256": SHA,
                        },
                        {
                            "table_name": "mart_strategic_ml_market_metric",
                            "row_count": 1,
                            "sha256": SHA,
                        },
                        {
                            "table_name": "mart_strategic_cd_brand_metric",
                            "row_count": 1,
                            "sha256": SHA,
                        },
                        {
                            "table_name": "mart_strategic_cd_market_metric",
                            "row_count": 1,
                            "sha256": SHA,
                        },
                    ],
                },
                "replacement": {
                    "reference_ids": ["ML17", "ML99"],
                    "candidate_ids": ["ML17"],
                    "policy": "append_or_approved_removal",
                    "removed_id_approval": {
                        "approved": True,
                        "removed_ids": ["ML99"],
                        "approver": "pl@example.com",
                        "reason": "definition removed from approved reference",
                    },
                    "reference_report": {
                        "mart_references": {"ML99": []},
                        "cache_references": {"ML99": []},
                        "saved_filter_references": {"ML99": []},
                        "inactive_decisions": [],
                    },
                },
            },
            "cache_refresh": {"target_tables": ["cache_brands", "cache_market_status"]},
        }
    )
    return payload


def _add_publish_plan(payload: dict[str, object], tmp_path: Path) -> dict[str, object]:
    workspace_payload = payload["workspace"]
    identity_payload = payload["identity"]
    assert isinstance(workspace_payload, dict)
    assert isinstance(identity_payload, dict)
    candidate_dir = Path(str(workspace_payload["candidate_root"]))
    live_dir = tmp_path / "live"
    backup_dir = tmp_path / "publish-backups"
    live_dir.mkdir()
    backup_dir.mkdir()
    payload["workspace"] = {
        "candidate_root": str(candidate_dir),
        "backup_root": str(backup_dir / str(identity_payload["run_id"])),
        "journal_path": str(workspace_payload["journal_path"]),
    }
    payload["publish_plan"] = {
        "candidate": {
            "candidate_id": identity_payload["run_id"],
            "mi_master_sha256": identity_payload["mi_master_sha256"],
            "manifest_sha256": identity_payload["catalog_diff_hash"],
            "allowed_cache_tables": ["cache_brands", "cache_market_status"],
        },
        "candidate_dir": str(candidate_dir),
        "live_dir": str(live_dir),
        "backup_dir": str(backup_dir),
        "journal_path": str(workspace_payload["journal_path"]),
        "corpus": {"candidate_dir": str(candidate_dir), "backup_dir": str(backup_dir)},
        "approval_identity": identity_payload,
    }
    return payload


def test_default_prepare_cli_fails_closed_without_real_stage_binding(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    _write_request(request_path, request.as_dict())

    assert main(["prepare", "--request-json", str(request_path), "--ledger-sqlite", str(ledger_path)]) == 1

    ledger = open_sqlite_ledger(ledger_path)
    entry = ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    events = ledger.stage_events(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert [(event.stage, event.status) for event in events] == [("catalog_sync", "failed")]
    assert not any(request.workspace.candidate_root.glob("*.json"))
    assert ledger.prepared_candidate(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    ) is None


def test_pipeline_prepare_adapter_calls_bound_concrete_function(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    calls: list[str] = []

    def catalog_sync(_request) -> None:
        calls.append("catalog_sync")

    PipelinePrepareAdapters(
        PrepareFunctionBindings(catalog_sync=catalog_sync)
    ).catalog_sync(request)

    assert calls == ["catalog_sync"]


def test_pipeline_prepare_adapter_propagates_bound_function_failure(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)

    def catalog_sync(_request) -> None:
        raise RuntimeError("actual catalog sync failed")

    with pytest.raises(RuntimeError, match="actual catalog sync failed"):
        PipelinePrepareAdapters(
            PrepareFunctionBindings(catalog_sync=catalog_sync)
        ).catalog_sync(request)


def test_pipeline_prepare_adapter_fails_closed_when_stage_unbound(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)

    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: scope_plan"):
        PipelinePrepareAdapters(PrepareFunctionBindings()).scope_plan(request)


def test_pipeline_publish_adapters_fail_closed_when_unbound(tmp_path: Path) -> None:
    target = workspace(tmp_path)
    refresh = PipelineCacheRefresher()
    invalidate = PipelineRuntimeCatalogInvalidator()

    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: mart_publish"):
        PipelinePublisher().publish(target, identity())
    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: cache_refresh"):
        refresh.refresh_tables(("cache_brands", "cache_market_status"))
    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: catalog_invalidate"):
        invalidate.invalidate(identity())

    assert not target.backup_root.exists()


def test_pipeline_publish_adapter_calls_bound_concrete_function(tmp_path: Path) -> None:
    target = workspace(tmp_path)
    calls: list[Path] = []

    def publish_fn(publish_workspace, _identity):
        calls.append(publish_workspace.candidate_root)
        from pipeline.scripts.ingest_hook.mi_master_definition_contract import PublishReceipt

        return PublishReceipt(publish_workspace.journal_path, publish_workspace.backup_root)

    receipt = PipelinePublisher(publish_fn).publish(target, identity())

    assert calls == [target.candidate_root]
    assert receipt.journal_path == target.journal_path


def test_s2_catalog_sync_adapter_calls_checked_in_s2_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = prepare_request(tmp_path)
    catalog_root = tmp_path / "catalog"
    catalog_sync = PipelineCatalogSync(
        output_root=tmp_path / "s2-output",
        input_file=tmp_path / "mi-master.xlsx",
        catalog_root=catalog_root,
        target_db="jw_mart_candidate",
        sync_catalog_db="jw_catalog_sync",
        cache_dir=tmp_path / "cache",
        inputs_dir=tmp_path / "inputs",
        ubist_dir=tmp_path / "ubist",
        iqvia_nsa_dir=tmp_path / "iqvia",
    )
    request = type(request)(
        identity=request.identity,
        workspace=request.workspace,
        market_ordinal=request.market_ordinal,
        catalog_sync=catalog_sync,
    )
    calls: list[dict[str, object]] = []

    def run(params: dict[str, object]) -> int:
        calls.append(params)
        catalog_root.mkdir()
        return 0

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.mi_master_definition_commands.s2_catalog.run",
        run,
    )

    run_s2_catalog_sync(request)

    assert len(calls) == 1
    assert calls[0]["target_dir"] == catalog_sync.output_root
    assert calls[0]["input_file"] == catalog_sync.input_file
    assert calls[0]["catalog_root"] == catalog_root


def test_scope_plan_adapter_calls_real_planner_and_persists_its_output(tmp_path: Path) -> None:
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, _full_pipeline_payload(tmp_path))
    request = load_definition_request(request_path)

    run_scope_plan(request)

    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan == {
        "market_ids": ["ML17"],
        "cache_tables": ["cache_brands", "cache_market_status"],
        "general_rebuild_atc4": ["A10B"],
        "affected_ml_ids": ["ML17"],
        "affected_cd_ids": ["CD17"],
        "unchanged_ml_ids": ["ML18"],
        "unchanged_cd_ids": ["CD18"],
    }


def test_candidate_build_adapter_calls_scoped_s5_with_typed_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, _full_pipeline_payload(tmp_path))
    request = load_definition_request(request_path)
    calls: list[dict[str, object]] = []

    def run(params: dict[str, object]) -> int:
        calls.append(params)
        return 0

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.mi_master_definition_commands.s5_mart.run",
        run,
    )

    run_candidate_build(request)

    assert calls == [
        {
            "target_db": "jw_mart_candidate",
            "source_db": "jw_mart_source",
            "general_source_db": "jw_mart_general",
            "catalog_root": tmp_path / "catalog",
            "affected_ml_ids": ("ML17",),
            "affected_cd_ids": ("CD17",),
        }
    ]


def test_candidate_build_adapter_propagates_scoped_s5_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, _full_pipeline_payload(tmp_path))
    request = load_definition_request(request_path)
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.mi_master_definition_commands.s5_mart.run",
        lambda _params: 7,
    )

    with pytest.raises(RuntimeError, match="scoped S5 candidate build failed rc=7"):
        run_candidate_build(request)


def test_sigma_and_post_gate_adapters_call_real_validation_functions(tmp_path: Path) -> None:
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, _full_pipeline_payload(tmp_path))
    request = load_definition_request(request_path)

    run_sigma(request)
    run_post_gate(request)


def test_sigma_adapter_propagates_real_validation_failure(tmp_path: Path) -> None:
    payload = _full_pipeline_payload(tmp_path)
    validation = payload["validation"]
    assert isinstance(validation, dict)
    validation["unchanged_market_hash_after"] = {"ML18": "d" * 64}
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, payload)
    request = load_definition_request(request_path)

    with pytest.raises(ValueError, match="unchanged market hash changed: ML18"):
        run_sigma(request)


def test_publisher_from_request_calls_real_atomic_publish_candidate(tmp_path: Path) -> None:
    payload = _add_publish_plan(_full_pipeline_payload(tmp_path), tmp_path)
    workspace_payload = payload["workspace"]
    assert isinstance(workspace_payload, dict)
    candidate_dir = Path(str(workspace_payload["candidate_root"]))
    publish_plan = payload["publish_plan"]
    assert isinstance(publish_plan, dict)
    live_dir = Path(str(publish_plan["live_dir"]))
    backup_dir = Path(str(publish_plan["backup_dir"]))
    (live_dir / "live.txt").write_text("old", encoding="utf-8")
    (candidate_dir / "candidate.txt").write_text("new", encoding="utf-8")
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, payload)
    parsed = load_definition_request(request_path)

    receipt = publisher_from_request(parsed).publish(parsed.workspace, parsed.identity)

    assert receipt.backup_root == backup_dir / "run-mi-master-1"
    assert (live_dir / "candidate.txt").read_text(encoding="utf-8") == "new"
    assert (backup_dir / "run-mi-master-1" / "live.txt").read_text(encoding="utf-8") == "old"


def test_prepare_candidate_payload_binds_canonical_publish_plan(tmp_path: Path) -> None:
    payload = _add_publish_plan(_full_pipeline_payload(tmp_path), tmp_path)
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    _write_request(request_path, payload)
    request = load_definition_request(request_path)
    ledger = open_sqlite_ledger(ledger_path)

    assert prepare_definition_refresh_candidate(ledger, request, RecordingPrepareAdapters([])) == 0

    candidate = ledger.prepared_candidate(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert candidate is not None
    assert candidate.payload["publish_plan"] == payload["publish_plan"]


def test_prepare_rejects_publish_plan_that_does_not_match_workspace(tmp_path: Path) -> None:
    payload = _add_publish_plan(_full_pipeline_payload(tmp_path), tmp_path)
    workspace_payload = payload["workspace"]
    assert isinstance(workspace_payload, dict)
    workspace_payload["backup_root"] = str(tmp_path / "wrong-backup")
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    _write_request(request_path, payload)
    request = load_definition_request(request_path)

    assert prepare_definition_refresh_candidate(
        open_sqlite_ledger(ledger_path),
        request,
        RecordingPrepareAdapters([]),
    ) == 1


def test_default_approved_publish_cli_preflights_invalidator_before_publish_side_effects(
    tmp_path: Path,
) -> None:
    payload = _add_publish_plan(_full_pipeline_payload(tmp_path), tmp_path)
    publish_plan = payload["publish_plan"]
    assert isinstance(publish_plan, dict)
    live_dir = Path(str(publish_plan["live_dir"]))
    backup_dir = Path(str(publish_plan["backup_dir"]))
    (live_dir / "live.txt").write_text("old", encoding="utf-8")
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    _write_request(request_path, payload)
    request = load_definition_request(request_path)
    ledger = open_sqlite_ledger(ledger_path)
    assert prepare_definition_refresh_candidate(ledger, request, RecordingPrepareAdapters([])) == 0
    approve(ledger, request)

    assert main(
        ["approved-publish", "--request-json", str(request_path), "--ledger-sqlite", str(ledger_path)]
    ) == 1

    entry = ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert "NOT_IMPLEMENTED: catalog_invalidate" in (entry.reason or "")
    assert (live_dir / "live.txt").read_text(encoding="utf-8") == "old"
    assert not (backup_dir / "cand-17").exists()


def test_runtime_catalog_invalidation_remains_final_fail_closed_without_checked_in_hook(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, _full_pipeline_payload(tmp_path))
    request = load_definition_request(request_path)

    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: catalog_invalidate"):
        PipelineRuntimeCatalogInvalidator.from_request(request).invalidate(request.identity)


def test_cache_refresher_rejects_request_tables_outside_refresh_allowlist(tmp_path: Path) -> None:
    payload = _full_pipeline_payload(tmp_path)
    payload["cache_refresh"] = {"target_tables": ["cache_brands", "cache_cause"]}
    request_path = tmp_path / "definition-request.json"
    _write_request(request_path, payload)
    request = load_definition_request(request_path)

    with pytest.raises(RuntimeError, match="forbidden cache refresh table"):
        PipelineCacheRefresher.from_request(request).refresh_tables(("cache_brands", "cache_market_status"))


def test_cli_e2e_17th_market_never_greens_by_marker_generation(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    payload = request.as_dict()
    payload["market_ordinal"] = 17
    payload["commands"] = {"catalog_sync": ["python", "-c", "raise SystemExit(88)"]}
    _write_request(request_path, payload)

    assert main(["prepare", "--request-json", str(request_path), "--ledger-sqlite", str(ledger_path)]) == 1

    parsed_request = load_definition_request(request_path)
    ledger = open_sqlite_ledger(ledger_path)
    entry = ledger.status(
        parsed_request.identity.ledger_epoch,
        CATEGORY,
        parsed_request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert not (parsed_request.workspace.candidate_root / "catalog_sync.json").exists()
    assert not (parsed_request.workspace.backup_root / "publish_receipt.json").exists()


def test_ubist_expected_stages_remain_structurally_unchanged() -> None:
    assert _StageTracker.STAGES == (
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
    )
    assert expected_stages(resolve_category("ubist")) == [
        {"stage": stage, "seq": seq, "applicable": True}
        for seq, stage in enumerate(_StageTracker.STAGES, start=1)
    ]


def test_mi_definition_category_uses_source_specific_expected_stages() -> None:
    spec = resolve_category(CATEGORY)

    assert spec.production_load_supported is False
    assert expected_stages(spec) == [
        {"stage": stage, "seq": seq, "applicable": True}
        for seq, stage in enumerate(
            (
                "catalog_sync",
                "scope_plan",
                "candidate_build",
                "sigma",
                "post_gate",
                "awaiting_approval",
                "mart_publish",
                "cache_refresh",
                "catalog_invalidate",
            ),
            start=1,
        )
    ]


def test_generic_job_runner_cannot_green_mi_definition_submission(
    sqlite_ledger, tmp_path: Path
) -> None:
    manifest_path = write_submission(tmp_path, category=CATEGORY)

    rc = job_runner.run(
        manifest_path,
        input_root=tmp_path,
        ledger=sqlite_ledger,
        rehearsal_root=None,
        run_id="generic-mi-definition-run",
    )

    entry = sqlite_ledger.status("2026-07", CATEGORY, _sha(manifest_path))
    assert rc == 1
    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert entry.status != STATUS_COMPLETE
    assert "typed definition-refresh CLI" in (entry.reason or "")


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
