"""R14 — the dense fast-path thresholds are operator settings, and declining
the fast path is recorded rather than silent."""
from __future__ import annotations

import importlib
import logging

import pytest

from src import xlsx_sql_route


@pytest.fixture(name="reloaded")
def _reloaded(monkeypatch):
    def _load(**env: str):
        for key in (
            "FILE_SQL_DENSE_MAX_XML_BYTES",
            "FILE_SQL_FAST_PROFILE_MIN_XML_BYTES",
            "FILE_SQL_FAST_PROFILE_MAX_XML_BYTES",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(xlsx_sql_route)

    yield _load
    importlib.reload(xlsx_sql_route)


def test_thresholds_fall_back_to_documented_defaults(reloaded):
    module = reloaded()
    assert module.DENSE_SQL_MAX_XML_BYTES == 128 * 1024 * 1024
    assert module.FAST_SQL_PROFILE_MIN_XML_BYTES == 32 * 1024 * 1024
    assert module.FAST_SQL_PROFILE_MAX_XML_BYTES == 256 * 1024 * 1024


def test_each_threshold_is_settings_injected(reloaded):
    module = reloaded(
        FILE_SQL_DENSE_MAX_XML_BYTES="1000",
        FILE_SQL_FAST_PROFILE_MIN_XML_BYTES="2000",
        FILE_SQL_FAST_PROFILE_MAX_XML_BYTES="3000",
    )
    assert module.DENSE_SQL_MAX_XML_BYTES == 1000
    assert module.FAST_SQL_PROFILE_MIN_XML_BYTES == 2000
    assert module.FAST_SQL_PROFILE_MAX_XML_BYTES == 3000


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "12.5"])
def test_an_unusable_threshold_warns_and_keeps_the_default(reloaded, caplog, raw):
    # A silently-zero limit would disable the fast path for every workbook, so a
    # bad value must be visible rather than absorbed.
    with caplog.at_level(logging.WARNING):
        module = reloaded(FILE_SQL_DENSE_MAX_XML_BYTES=raw)
    assert module.DENSE_SQL_MAX_XML_BYTES == 128 * 1024 * 1024
    assert "threshold ignored" in caplog.text
    assert "FILE_SQL_DENSE_MAX_XML_BYTES" in caplog.text


def test_declining_the_dense_path_records_the_reason(caplog):
    profile = xlsx_sql_route.SheetSqlProfile(
        sheet_index=1,
        sheet_name="Sell Out  Standard",
        sheet_path="xl/worksheets/sheet1.xml",
        row_count=12269,
        column_count=252,
        used_cell_count=1_800_000,
        formula_cell_count=0,
        merged_range_count=0,
        proven_dense=True,
    )
    with caplog.at_level(logging.INFO):
        xlsx_sql_route._log_dense_declined(
            profile, "xml_over_dense_limit", xml_bytes=124_780_064, limit_bytes=1000
        )
    assert "dense_path_declined" in caplog.text
    assert "reason=xml_over_dense_limit" in caplog.text
    assert "fallback=streaming_parser" in caplog.text
    assert "xml_bytes=124780064" in caplog.text
