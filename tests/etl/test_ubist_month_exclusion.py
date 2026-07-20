"""Unit tests for UBIST s1 month exclusion (R-1 sidecar collision fix).

These exercise the pure filtering seam and the CLI plumbing without touching
the heavy parquet write path, so they stay fast and deterministic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.etl.io.ubist_loader import iter_included_xlsx_rows
from pipeline.etl.run import parse_args


def _fake_rows(_path: Path, _lookup: object):
    yield "2026-04", {"id": 1}
    yield "2026-05", {"id": 2}
    yield "2026-05", {"id": 3}
    yield "2026-06", {"id": 4}


def test_iter_included_skips_only_excluded_periods() -> None:
    out = list(
        iter_included_xlsx_rows(
            Path("x.xlsx"), None, frozenset({"2026-05"}), _row_source=_fake_rows
        )
    )
    # Excluded month dropped entirely; every other row preserved in order.
    assert out == [("2026-04", {"id": 1}), ("2026-06", {"id": 4})]


def test_iter_included_default_keeps_everything() -> None:
    out = list(
        iter_included_xlsx_rows(Path("x.xlsx"), None, frozenset(), _row_source=_fake_rows)
    )
    assert out == [
        ("2026-04", {"id": 1}),
        ("2026-05", {"id": 2}),
        ("2026-05", {"id": 3}),
        ("2026-06", {"id": 4}),
    ]


def test_iter_included_can_exclude_multiple_periods() -> None:
    out = list(
        iter_included_xlsx_rows(
            Path("x.xlsx"), None, frozenset({"2026-04", "2026-06"}), _row_source=_fake_rows
        )
    )
    assert [period for period, _ in out] == ["2026-05", "2026-05"]


def test_run_parses_repeatable_exclude_ubist_month() -> None:
    args = parse_args(
        [
            "--stage",
            "s1",
            "--source",
            "ubist",
            "--exclude-ubist-month",
            "2026-05",
            "--exclude-ubist-month",
            "2026-04",
        ]
    )
    assert args.exclude_ubist_month == ["2026-05", "2026-04"]


def test_run_defaults_exclude_ubist_month_to_empty() -> None:
    args = parse_args(["--stage", "s1", "--source", "ubist"])
    assert args.exclude_ubist_month == []


def test_run_rejects_malformed_exclude_ubist_month() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--stage", "s1", "--exclude-ubist-month", "2026/05"])
