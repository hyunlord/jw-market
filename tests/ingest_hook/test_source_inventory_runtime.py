from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.source_inventory import (
    FileObservation,
    ScanSnapshot,
    SourceInventoryError,
    write_inventory_snapshot,
)
from pipeline.scripts.ingest_hook.source_inventory_runtime import (
    load_scan_policy,
    latest_successful_snapshot,
)


def test_load_scan_policy_requires_an_explicit_absolute_source_root(monkeypatch) -> None:
    monkeypatch.setenv(
        "INGEST_SOURCE_SCAN_POLICIES_JSON",
        json.dumps({"ubist": {"root": "relative", "period_unit": "month"}}),
    )

    with pytest.raises(SourceInventoryError, match="absolute"):
        load_scan_policy("ubist", required=True)


def test_load_scan_policy_keeps_each_source_contract_distinct(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "INGEST_SOURCE_SCAN_POLICIES_JSON",
        json.dumps(
            {
                "ubist": {"root": str(tmp_path / "ubist"), "period_unit": "month"},
                "iqvia_nsa": {
                    "root": str(tmp_path / "nsa"),
                    "period_unit": "quarter",
                    "excluded_relative_roots": ["demo", "quarantine"],
                },
            }
        ),
    )

    policy = load_scan_policy("iqvia_nsa", required=True)

    assert policy.category == "iqvia_nsa"
    assert policy.root == (tmp_path / "nsa").resolve()
    assert policy.period_unit == "quarter"
    assert policy.excluded_relative_roots == ("demo", "quarantine")


def test_latest_successful_snapshot_uses_observed_time_not_filename(tmp_path: Path) -> None:
    root = tmp_path / "inventory"
    older = ScanSnapshot(
        "1", "ubist", "2026-05", "a" * 64, "z-run", "2026-08-06T00:00:00Z",
        (FileObservation("a.xlsx", "1" * 64, 1, "classified", "ubist", 1, ("2026-05",)),),
    )
    newer = ScanSnapshot(
        "1", "ubist", "2026-06", "b" * 64, "a-run", "2026-08-07T00:00:00Z",
        (FileObservation("b.xlsx", "2" * 64, 1, "classified", "ubist", 1, ("2026-06",)),),
    )
    write_inventory_snapshot(older, root)
    write_inventory_snapshot(newer, root)

    restored = latest_successful_snapshot(root, "ubist")

    assert restored is not None
    assert restored.run_id == "a-run"
