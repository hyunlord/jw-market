from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.job_runner import _StageTracker, expected_stages
from pipeline.scripts.ingest_hook.ledger import STATUS_FAILED, open_sqlite_ledger
from pipeline.scripts.ingest_hook.mi_master_definition_commands import (
    PipelineCacheRefresher,
    PipelineCatalogSync,
    PipelinePrepareAdapters,
    PipelinePublisher,
    PipelineRuntimeCatalogInvalidator,
    PrepareFunctionBindings,
    run_s2_catalog_sync,
)
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    CATEGORY,
    load_definition_request,
    main,
)
from mi_master_definition_fixtures import identity, prepare_request, workspace


def _write_request(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


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
