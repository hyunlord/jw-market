from __future__ import annotations

from pathlib import Path

from pipeline.scripts.audit.verify_l1_iqvia import expected_quarters, quarter_label
from pipeline.scripts.audit.verify_l1_runner import summarize_status_counts
from pipeline.scripts.audit.verify_l1_ubist import expected_months, partition_period_from_path


def test_expected_months_is_inclusive() -> None:
    assert expected_months("2021-11", "2022-02") == ["2021-11", "2021-12", "2022-01", "2022-02"]


def test_partition_period_from_hive_path() -> None:
    path = Path("output/ubist/year=2026/month=04/data.parquet")

    assert partition_period_from_path(path) == "2026-04"


def test_expected_iqvia_quarters_are_inclusive() -> None:
    quarters = expected_quarters((2020, 3), (2025, 4))

    assert len(quarters) == 22
    assert quarters[0] == "2020Q3"
    assert quarters[-1] == "2025Q4"


def test_quarter_label_normalizes_database_tuple() -> None:
    assert quarter_label(2025, 4) == "2025Q4"


def test_summarize_status_counts_defaults_missing_status_to_info() -> None:
    checks = [
        {"name": "a", "status": "PASS"},
        {"name": "b", "status": "WARN"},
        {"name": "c"},
    ]

    assert summarize_status_counts(checks) == {"INFO": 1, "PASS": 1, "WARN": 1}
