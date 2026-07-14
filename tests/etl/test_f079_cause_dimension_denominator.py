from __future__ import annotations

import pytest

from pipeline.scripts.etl import build_cache_cause as cause
from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
)


PERIODS = ["2026-05"]


def _row(*, name: str, value: float, field: str, label: str | None) -> dict[str, object]:
    return {
        "brand_name": name,
        "brand_key": name,
        "metric_history": {"2026-05": {"raw_value": value}},
        "by_dimension": {field: label},
        "dimension_data": {field: {}},
        "overlay_data": {"is_class_excluded": False},
    }


@pytest.mark.parametrize(("level", "field"), [("Class", "class"), ("Molecule", "molecule")])
def test_missing_partition_dimension_is_disclosed_without_share_renormalization(
    level: str,
    field: str,
) -> None:
    segments = cause._segment_rows_for_level(
        rows=[
            _row(name="분류됨", value=90.0, field=field, label="A"),
            _row(name="차원누락", value=10.0, field=field, label=None),
        ],
        level=level,
        periods=PERIODS,
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    by_name = {segment["name"]: segment for segment in segments}
    assert by_name["A"]["value_series"] == [90.0]
    assert by_name["A"]["series_pct"] == [90.0]
    assert by_name["미분류"]["value_series"] == [10.0]
    assert by_name["미분류"]["series_pct"] == [10.0]
    assert by_name["미분류"]["data_quality"] == {
        "available": False,
        "reason": "dimension_value_missing",
        "dimension": level,
    }
    assert sum(segment["value_series"][0] for segment in segments) == 100.0
    assert sum(segment["series_pct"][0] for segment in segments) == 100.0


def test_complete_ox_gx_partition_remains_byte_equivalent() -> None:
    rows = [
        _row(name="오리지널", value=70.0, field="ox_gx", label="Ox"),
        _row(name="제네릭", value=30.0, field="ox_gx", label="Gx"),
    ]

    segments = cause._segment_rows_for_level(
        rows=rows,
        level="Ox/Gx",
        periods=PERIODS,
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    assert segments == [
        {
            "name": "Ox",
            "rank": 1,
            "recent_share_pct": 70.0,
            "series_pct": [70.0],
            "value_series": [70.0],
        },
        {
            "name": "Gx",
            "rank": 2,
            "recent_share_pct": 30.0,
            "series_pct": [30.0],
            "value_series": [30.0],
        },
    ]


def test_precomputed_block_contract_invalidates_silent_drop_payloads() -> None:
    assert ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION == "analysis-level-block-v5-unclassified-partitions"
