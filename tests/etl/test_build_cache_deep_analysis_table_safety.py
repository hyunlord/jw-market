from __future__ import annotations

import pytest

from pipeline.scripts.etl.build_cache_deep_analysis import _safe_table_name


def test_safe_table_name_rejects_live_cache_target() -> None:
    with pytest.raises(SystemExit, match="live cache table"):
        _safe_table_name("cache_deep_analysis")


def test_safe_table_name_accepts_staging_target() -> None:
    assert (
        _safe_table_name("cache_deep_analysis_events_staging_20260712010000")
        == "cache_deep_analysis_events_staging_20260712010000"
    )
