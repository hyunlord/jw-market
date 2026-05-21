from __future__ import annotations

from pathlib import Path

from pipeline.scripts.audit.verify_l2_enriched import (
    expected_ml_ids,
    partition_ml_id_from_path,
    status_for_total_l2,
)
from pipeline.scripts.audit.verify_l2_runner import summarize_status_counts


def test_expected_ml_ids_are_zero_padded() -> None:
    assert expected_ml_ids()[:3] == ["ml_001", "ml_002", "ml_003"]
    assert expected_ml_ids()[-1] == "ml_016"


def test_partition_ml_id_from_enriched_path() -> None:
    path = Path("output/enriched/ml_id=ml_006/data.parquet")

    assert partition_ml_id_from_path(path) == "ml_006"


def test_l2_total_status_accepts_expected_window() -> None:
    assert status_for_total_l2(73_298_824) == "PASS"
    assert status_for_total_l2(75_000_000) == "WARN"


def test_summarize_status_counts_defaults_missing_status_to_info() -> None:
    checks = [
        {"name": "ok", "status": "PASS"},
        {"name": "warn", "status": "WARN"},
        {"name": "info"},
    ]

    assert summarize_status_counts(checks) == {"INFO": 1, "PASS": 1, "WARN": 1}
