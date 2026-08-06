from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.job_runner import _StageTracker, expected_stages
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, open_sqlite_ledger
from pipeline.scripts.ingest_hook.mi_master_definition_commands import LocalPrepareAdapters
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    CATEGORY,
    STAGES,
    load_definition_request,
    main,
)
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    assert_complete_stage_contract,
)
from mi_master_definition_fixtures import approve, prepare_request


def _write_request(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_local_prepare_adapter_fails_closed_when_stage_precondition_missing(
    tmp_path: Path,
) -> None:
    request = prepare_request(tmp_path)

    with pytest.raises(RuntimeError, match="NOT_IMPLEMENTED: catalog_sync"):
        LocalPrepareAdapters().scope_plan(request)


def test_cli_e2e_17th_market_uses_checked_in_python_adapters(tmp_path: Path) -> None:
    request = prepare_request(tmp_path)
    request_path = tmp_path / "definition-request.json"
    ledger_path = tmp_path / "ledger.db"
    payload = request.as_dict()
    payload["market_ordinal"] = 17
    payload["commands"] = {"catalog_sync": ["python", "-c", "raise SystemExit(88)"]}
    _write_request(request_path, payload)

    assert main(["prepare", "--request-json", str(request_path), "--ledger-sqlite", str(ledger_path)]) == 0

    parsed_request = load_definition_request(request_path)
    for stage in STAGES[:5]:
        assert (parsed_request.workspace.candidate_root / f"{stage}.json").is_file()
    catalog_payload = json.loads(
        (parsed_request.workspace.candidate_root / "catalog_sync.json").read_text(
            encoding="utf-8"
        )
    )
    assert catalog_payload["market_ordinal"] == 17

    ledger = open_sqlite_ledger(ledger_path)
    approve(ledger, parsed_request)

    assert main(
        ["approved-publish", "--request-json", str(request_path), "--ledger-sqlite", str(ledger_path)]
    ) == 0

    entry = ledger.status(
        parsed_request.identity.ledger_epoch,
        CATEGORY,
        parsed_request.identity.catalog_diff_hash,
    )
    assert entry is not None
    assert entry.status == STATUS_COMPLETE
    assert (parsed_request.workspace.backup_root / "publish_receipt.json").is_file()
    assert (parsed_request.workspace.candidate_root / "cache_refresh.json").is_file()
    assert (parsed_request.workspace.candidate_root / "catalog_invalidate.json").is_file()
    cache_payload = json.loads(
        (parsed_request.workspace.candidate_root / "cache_refresh.json").read_text(
            encoding="utf-8"
        )
    )
    assert cache_payload["tables"] == ["cache_brands", "cache_market_status"]
    assert "cache_cause" not in cache_payload["tables"]
    assert "cache_deep_analysis" not in cache_payload["tables"]
    assert_complete_stage_contract(
        ledger.stage_events(
            parsed_request.identity.ledger_epoch,
            CATEGORY,
            parsed_request.identity.catalog_diff_hash,
        )
    )


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
