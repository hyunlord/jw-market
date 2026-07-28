from __future__ import annotations

from collections.abc import Mapping
import json
import logging

import pytest

from jw_chat_agent_poc.tools.query_layer.compute import metric_render_data
from jw_chat_agent_poc.tools.query_layer.mart_json import MartJsonPoint
from jw_chat_agent_poc.tools.query_layer.store import (
    MartRecord,
    MartSnapshot,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ml_id": "ml_test",
        "brand_name": "테스트",
        "source": "ubist",
        "measure": "sales",
        "metric_history": json.dumps({"2026-05": {"raw_value": 100.0}}),
        "channel_data": json.dumps({"의원": {"2026-05": {"raw_value": 60.0}}}),
        "specialty_data": json.dumps({"내과": {"2026-05": {"raw_value": 40.0}}}),
        "dimension_data": json.dumps({"제형": {"정": {"2026-05": {"raw_value": 20.0}}}}),
        "by_dimension": json.dumps({"company": "테스트제약", "molecule": "성분"}),
    }
    row.update(overrides)
    return row


def test_from_row_compacts_point_rows_in_all_value_columns() -> None:
    record = MartRecord.from_row(_row())

    points = (
        record.metric_history["2026-05"],
        record.channel_data["의원"]["2026-05"],
        record.specialty_data["내과"]["2026-05"],
        record.dimension_data["제형"]["정"]["2026-05"],
    )
    assert all(isinstance(point, Mapping) for point in points)
    assert all(not isinstance(point, dict) for point in points)
    assert [point["raw_value"] for point in points] == [100.0, 60.0, 40.0, 20.0]
    assert record.by_dimension == {"company": "테스트제약", "molecule": "성분"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"raw_value": 1.0}, {"raw_value": 1.0}),
        (
            {"raw_value": 1.0, "ms": 2.0, "source_status": "query_failed"},
            {"raw_value": 1.0, "ms": 2.0, "source_status": "query_failed"},
        ),
        (
            {"brand": "테스트", "rank": 3, "raw_value": 1.0, "ms": 2.0},
            {"brand": "테스트", "rank": 3, "raw_value": 1.0, "ms": 2.0},
        ),
        (
            {
                "growth_abs": 1.0,
                "mat": 2.0,
                "mom": 3.0,
                "ms": 4.0,
                "qoq": 5.0,
                "rank": 6,
                "raw_value": 7.0,
                "yoy": 8.0,
            },
            {
                "growth_abs": 1.0,
                "mat": 2.0,
                "mom": 3.0,
                "ms": 4.0,
                "qoq": 5.0,
                "rank": 6,
                "raw_value": 7.0,
                "yoy": 8.0,
            },
        ),
    ],
)
def test_partial_point_shapes_preserve_present_keys(
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    record = MartRecord.from_row(_row(metric_history=json.dumps({"2026-05": payload})))
    point = record.metric_history["2026-05"]

    assert dict(point) == expected


def test_source_status_survives_compaction() -> None:
    record = MartRecord.from_row(
        _row(
            metric_history=json.dumps(
                {"2026-05": {"raw_value": 1.0, "source_status": "query_failed"}}
            )
        )
    )
    snapshot = MartSnapshot((record,), loaded_at=0.0)

    assert snapshot.value_status(record, "2026-05") == "query_failed"
    assert snapshot.value_or_none(record, "2026-05") is None


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"raw_value": 1.0, "status": "query_failed"}, "query_failed"),
        (
            {
                "raw_value": 1.0,
                "status": "query_failed",
                "source_status": "OK",
            },
            "OK",
        ),
        (
            {
                "raw_value": 1.0,
                "status": "query_failed",
                "source_status": None,
            },
            "OK",
        ),
    ],
)
def test_status_fallback_matches_dict_semantics(
    payload: dict[str, object],
    expected_status: str,
) -> None:
    record = MartRecord.from_row(_row(metric_history=json.dumps({"2026-05": payload})))
    snapshot = MartSnapshot((record,), loaded_at=0.0)

    assert snapshot.value_status(record, "2026-05") == expected_status


def test_explicit_none_remains_distinct_from_absent_key() -> None:
    record = MartRecord.from_row(
        _row(
            metric_history=json.dumps(
                {
                    "2026-04": {"raw_value": None},
                    "2026-05": {"raw_value": 1.0, "ms": None},
                }
            )
        )
    )

    assert dict(record.metric_history["2026-04"]) == {"raw_value": None}
    assert dict(record.metric_history["2026-05"]) == {"raw_value": 1.0, "ms": None}
    assert "source_status" not in record.metric_history["2026-05"]


def test_unknown_point_keys_are_preserved_and_observable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    record = MartRecord.from_row(
        _row(metric_history=json.dumps({"2026-05": {"raw_value": 1.0, "future_key": 7}}))
    )

    assert dict(record.metric_history["2026-05"]) == {
        "raw_value": 1.0,
        "future_key": 7,
    }
    point = record.metric_history["2026-05"]
    assert isinstance(point, MartJsonPoint)
    assert point.unknown_key_count == 1
    assert "unknown mart JSON point keys" in caplog.text
    assert "future_key" in caplog.text


@pytest.mark.parametrize(
    ("column", "payload", "path"),
    [
        (
            "metric_history",
            {"2026-05": {"raw_value": 1.0, "future_metric": 1}},
            ("2026-05",),
        ),
        (
            "channel_data",
            {"의원": {"2026-05": {"raw_value": 1.0, "future_channel": 1}}},
            ("의원", "2026-05"),
        ),
        (
            "specialty_data",
            {"내과": {"2026-05": {"raw_value": 1.0, "future_specialty": 1}}},
            ("내과", "2026-05"),
        ),
        (
            "dimension_data",
            {"제형": {"정": {"2026-05": {"raw_value": 1.0, "future_dimension": 1}}}},
            ("제형", "정", "2026-05"),
        ),
        (
            "by_dimension",
            {"future_series": {"2026-05": {"raw_value": 1.0, "future_metadata": 1}}},
            ("future_series", "2026-05"),
        ),
    ],
)
def test_unknown_keys_are_visible_in_each_json_column(
    column: str,
    payload: dict[str, object],
    path: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    record = MartRecord.from_row(_row(**{column: json.dumps(payload)}))
    value: object = getattr(record, column)
    for key in path:
        assert isinstance(value, Mapping)
        value = value[key]

    assert isinstance(value, Mapping)
    assert len(value) == 2
    assert f"column={column}" in caplog.text


@pytest.mark.parametrize(
    "column",
    (
        "metric_history",
        "channel_data",
        "specialty_data",
        "dimension_data",
        "by_dimension",
    ),
)
@pytest.mark.parametrize("payload", (None, "null", "[]", "{}"))
def test_five_json_columns_keep_existing_empty_coercion(
    column: str,
    payload: object,
) -> None:
    record = MartRecord.from_row(_row(**{column: payload}))

    assert getattr(record, column) == {}


def test_nested_lists_and_objects_remain_intact() -> None:
    nested = {
        "future_container": [
            {"label": "A"},
            {"raw_value": 1.0},
        ]
    }
    record = MartRecord.from_row(_row(by_dimension=json.dumps(nested)))

    assert record.by_dimension == nested


def test_malformed_json_keeps_existing_decode_failure() -> None:
    with pytest.raises(json.JSONDecodeError):
        MartRecord.from_row(_row(metric_history="{"))


def test_stored_rank_does_not_replace_computed_strategic_rank() -> None:
    first = MartRecord.from_row(
        _row(
            brand_name="첫째",
            metric_history=json.dumps(
                {"2026-05": {"raw_value": 200_000_000.0, "rank": 99}}
            ),
        )
    )
    second = MartRecord.from_row(
        _row(
            brand_name="둘째",
            metric_history=json.dumps(
                {"2026-05": {"raw_value": 100_000_000.0, "rank": 1}}
            ),
        )
    )
    snapshot = MartSnapshot((first, second), loaded_at=0.0)

    assert [row["rank"] for row in snapshot.ranked_brands("ml_test", "2026-05")] == [1, 2]
    assert first.metric_history["2026-05"]["rank"] == 99


def test_slots_do_not_leak_into_public_render_serialization() -> None:
    record = MartRecord.from_row(_row())
    snapshot = MartSnapshot((record,), loaded_at=0.0)

    rendered = metric_render_data(
        snapshot,
        "ml_test",
        "ubist",
        record,
        "sales",
        "2026-05",
    )
    serialized = json.dumps(rendered, ensure_ascii=False, allow_nan=False)

    assert "RawValuePoint" not in serialized
    assert "MartJsonPoint" not in serialized
    assert "_MISSING" not in serialized
