from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.ledger import (
    STAGE_FAILED,
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PUBLISH_RUNNING,
)
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    CATEGORY,
    STAGES,
    WORKFLOW_REF_URI,
    prepare_definition_refresh_candidate,
    run_approved_definition_publish,
)
from pipeline.scripts.ingest_hook.mi_master_definition_commands import (
    PipelineRuntimeCatalogInvalidator,
)
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    MissingStageError,
    assert_complete_stage_contract,
)
from mi_master_definition_fixtures import (
    RecordingCacheRefresher,
    RecordingInvalidator,
    RecordingPrepareAdapters,
    RecordingPublisher,
    approve,
    prepare_request,
    publish_request,
)


def test_prepare_uses_non_file_workflow_uri_and_does_not_read_upload_manifest(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.scripts.ingest_hook import contract

    monkeypatch.setattr(
        contract,
        "load_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("manifest read")),
    )
    request = prepare_request(tmp_path)
    adapters = RecordingPrepareAdapters([])

    assert prepare_definition_refresh_candidate(sqlite_ledger, request, adapters) == 0

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_AWAITING_APPROVAL
    assert entry.manifest_path == WORKFLOW_REF_URI
    assert not Path(entry.manifest_path).is_file()
    candidate = sqlite_ledger.prepared_candidate(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert candidate is not None
    assert "upload_manifest" not in candidate.payload
    assert adapters.calls == list(STAGES[:5])


def test_prepare_records_only_stages_that_adapters_executed(sqlite_ledger, tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    adapters = RecordingPrepareAdapters([])

    assert prepare_definition_refresh_candidate(sqlite_ledger, request, adapters) == 0

    events = sqlite_ledger.stage_events(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert [event.stage for event in events] == list(STAGES[:6])
    assert [event.stage for event in events if event.status != STAGE_FAILED] == list(STAGES[:6])


@pytest.mark.parametrize("stage", STAGES)
def test_definition_refresh_stage_contract_reports_each_omitted_stage_only(
    sqlite_ledger, tmp_path: Path, stage: str
) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)
    run_approved_definition_publish(publish_request(sqlite_ledger, request))
    events = [
        event
        for event in sqlite_ledger.stage_events(
            request.identity.ledger_epoch,
            CATEGORY,
            request.identity.catalog_diff_hash,
        )
        if event.stage != stage
    ]

    with pytest.raises(MissingStageError) as excinfo:
        assert_complete_stage_contract(events)

    assert excinfo.value.missing == (stage,)


@pytest.mark.parametrize("stage", STAGES[:5])
def test_injected_prepare_stage_failure_records_failed_stage_and_never_completes(
    sqlite_ledger, tmp_path: Path, stage: str
) -> None:
    request = prepare_request(tmp_path)
    adapters = RecordingPrepareAdapters([], fail_at=stage)

    assert prepare_definition_refresh_candidate(sqlite_ledger, request, adapters) == 1

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    events = sqlite_ledger.stage_events(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    failed = [event for event in events if event.status == STAGE_FAILED]
    assert [event.stage for event in failed] == [stage]
    assert sqlite_ledger.prepared_candidate(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    ) is None


def test_publish_failure_records_failed_stage_and_never_marks_complete(
    sqlite_ledger, tmp_path: Path
) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)

    assert run_approved_definition_publish(
        publish_request(sqlite_ledger, request, publisher=RecordingPublisher([], fail=True))
    ) == 1

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    events = sqlite_ledger.stage_events(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert [(event.stage, event.status) for event in events if event.status == STAGE_FAILED] == [
        ("mart_publish", STAGE_FAILED)
    ]


def test_unbound_runtime_invalidator_preflight_blocks_publish_and_cache(
    sqlite_ledger, tmp_path: Path
) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)
    publisher = RecordingPublisher([])
    refresher = RecordingCacheRefresher([])

    assert run_approved_definition_publish(
        publish_request(
            sqlite_ledger,
            request,
            publisher=publisher,
            cache_refresher=refresher,
            invalidator=PipelineRuntimeCatalogInvalidator(),
        )
    ) == 1

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert publisher.calls == []
    assert refresher.requested == []
    assert not request.workspace.backup_root.exists()


def test_publish_refresh_scope_runtime_invalidation_and_transition_sequence(
    sqlite_ledger, tmp_path: Path
) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)
    publisher = RecordingPublisher([])
    refresher = RecordingCacheRefresher([])
    invalidator = RecordingInvalidator([], [])

    assert run_approved_definition_publish(
        publish_request(
            sqlite_ledger,
            request,
            publisher=publisher,
            cache_refresher=refresher,
            invalidator=invalidator,
        )
    ) == 0

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_COMPLETE
    transitions = sqlite_ledger.status_transitions(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert [transition.status for transition in transitions][-3:] == [
        STATUS_AWAITING_APPROVAL,
        STATUS_PUBLISH_RUNNING,
        STATUS_COMPLETE,
    ]
    assert refresher.requested == [("cache_brands", "cache_market_status")]
    assert "cache_cause" not in refresher.requested[0]
    assert "cache_deep_analysis" not in refresher.requested[0]
    assert invalidator.identities == [request.identity]
    assert invalidator.preflight_identities == [request.identity]
    assert publisher.calls == [request.workspace]
    assert_complete_stage_contract(
        sqlite_ledger.stage_events(
            request.identity.ledger_epoch,
            CATEGORY,
            request.identity.catalog_diff_hash,
        )
    )


def test_publish_rejects_extra_cache_refresh_tables(sqlite_ledger, tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)

    assert run_approved_definition_publish(
        publish_request(
            sqlite_ledger,
            request,
            cache_refresher=RecordingCacheRefresher(
                [], returned=("cache_brands", "cache_cause")
            ),
        )
    ) == 1

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED


def test_complete_requires_durable_stage_contract_even_if_stage_write_is_dropped(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = prepare_request(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, request, RecordingPrepareAdapters([]))
    approve(sqlite_ledger, request)
    original_record_stage = sqlite_ledger.record_stage

    def drop_catalog_invalidate_complete(*args, **kwargs) -> None:
        if kwargs.get("stage") == "catalog_invalidate" and kwargs.get("status") == "complete":
            return
        original_record_stage(*args, **kwargs)

    monkeypatch.setattr(sqlite_ledger, "record_stage", drop_catalog_invalidate_complete)

    assert run_approved_definition_publish(
        publish_request(
            sqlite_ledger,
            request,
            publisher=RecordingPublisher([]),
            cache_refresher=RecordingCacheRefresher([]),
            invalidator=RecordingInvalidator([], []),
        )
    ) == 1

    entry = sqlite_ledger.status(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert "missing MI Master definition refresh stages: catalog_invalidate" in (entry.reason or "")
