"""Conversation history must persist a mart point as an object, not as its repr.

The two history serializers pass default=str, so a point there never raised: it became a repr
string and its ms, rank, brand and unknown keys stopped being readable. JSER made that repr
deterministic; it did not put the keys back.

★ Reachability: no path is known to put a point into either payload today — the trace carries
derived metadata only, and both sites were instrumented across the suite with zero point payloads.
These tests therefore pin a GUARD. The half that matters most is the pair that proves the guard
did not break what those sites already carried: datetime, Decimal, UUID, set and friends must
still reach str(), and rows written in the old shape must still be readable.
"""

from __future__ import annotations

import datetime
import decimal
import json
import sys
import uuid

import pytest

from jw_chat_agent_poc.service import conversation_history as ch
from jw_chat_agent_poc.service import history_projection as hp
from jw_chat_agent_poc.tools.query_layer.mart_json import (
    MartJsonPoint,
    RawValuePoint,
    compact_mart_json,
    mart_json_default_or_str,
)


SERIALIZERS = (
    pytest.param(ch._json_dumps, id="conversation_history"),
    pytest.param(hp._json_dumps, id="history_projection"),
)

FULL = {"raw_value": 80.39, "ms": 3.76, "rank": 6, "source_status": "OK", "brand": "리바로"}
WITH_UNKNOWN = {"raw_value": 80.39, "brand_new_metric": 123.456, "another": "text"}

RAW_COLUMNS = {
    "metric_history": {"2026-05": {"raw_value": 80.39, "ms": 3.76, "rank": 6}},
    "channel_data": {"의원": {"2026-05": {"raw_value": 41.09}}},
    "specialty_data": {"분리되지 않은 내과": {"2026-05": {"raw_value": 30.93, "ms": 3.50}}},
    "dimension_data": {"atc4": {"C10A1": {"raw_value": 55.5}}},
    "by_dimension": {"C10A1": {"raw_value": 55.5, "ms": 0.555, "rank": 1}},
}


def point(payload: dict, *, column: str = "metric_history"):
    return compact_mart_json({"p": payload}, column=column)["p"]


# ------------------------------------------------- 계열 a: keys survive


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_a_every_key_survives_the_write(dumps) -> None:
    restored = json.loads(dumps({"latest_point": point(FULL)}))

    assert restored["latest_point"] == FULL
    assert sorted(restored["latest_point"]) == sorted(FULL)


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_a_the_value_is_an_object_not_a_string(dumps) -> None:
    """The whole defect: default=str turned the point into prose."""
    restored = json.loads(dumps({"latest_point": point(FULL)}))

    assert isinstance(restored["latest_point"], dict)
    assert "MartJsonPoint" not in dumps({"latest_point": point(FULL)})


@pytest.mark.parametrize("dumps", SERIALIZERS)
@pytest.mark.parametrize("key", sorted(FULL))
def test_a_each_individual_key_is_addressable(dumps, key: str) -> None:
    restored = json.loads(dumps({"latest_point": point(FULL)}))

    assert restored["latest_point"][key] == FULL[key]


# ------------------------------------------------- 계열 b: round trip


def test_b_conversation_history_round_trip_returns_the_object() -> None:
    """Write with _json_dumps, read with the reader the module actually uses."""
    written = ch._json_dumps({"latest_point": point(FULL)})

    restored = ch._json_object(written)

    assert restored["latest_point"] == FULL


def test_b_history_projection_round_trip_returns_the_object() -> None:
    written = hp._json_dumps({"latest_point": point(FULL)})

    restored = hp._json_loads(written)

    assert restored["latest_point"] == FULL


def test_b_a_nested_trace_shaped_payload_round_trips() -> None:
    payload = {
        "tools_called": ["get_brand_metric"],
        "conversation_slots": {"brand": "리바로", "period": "2026-05"},
        "latest": {"points": [point(FULL), point({"raw_value": 84.93})]},
    }

    restored = ch._json_object(ch._json_dumps(payload))

    assert restored["latest"]["points"][0] == FULL
    assert restored["latest"]["points"][1] == {"raw_value": 84.93}
    assert restored["conversation_slots"] == {"brand": "리바로", "period": "2026-05"}


# ------------------------------------------------- 계열 c: unknown keys


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_c_unknown_keys_are_persisted_as_real_keys(dumps) -> None:
    restored = json.loads(dumps({"latest_point": point(WITH_UNKNOWN)}))

    assert restored["latest_point"] == WITH_UNKNOWN
    assert restored["latest_point"]["brand_new_metric"] == 123.456


def test_c_unknown_keys_survive_the_round_trip() -> None:
    restored = ch._json_object(ch._json_dumps({"p": point(WITH_UNKNOWN)}))

    assert restored["p"]["another"] == "text"


# ------------------------------------------------- 계열 d: ★ the str() fallback must remain


class _Custom:
    def __str__(self) -> str:
        return "custom-str"


FALLBACK_CASES = [
    pytest.param(datetime.datetime(2026, 7, 28, 12, 0), "2026-07-28 12:00:00", id="datetime"),
    pytest.param(datetime.date(2026, 7, 28), "2026-07-28", id="date"),
    pytest.param(decimal.Decimal("80.39"), "80.39", id="Decimal"),
    pytest.param(
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "12345678-1234-5678-1234-567812345678",
        id="UUID",
    ),
    pytest.param(_Custom(), "custom-str", id="custom-class"),
]


@pytest.mark.parametrize("dumps", SERIALIZERS)
@pytest.mark.parametrize(("value", "expected"), FALLBACK_CASES)
def test_d_objects_that_relied_on_str_still_reach_str(dumps, value, expected: str) -> None:
    """★ The regression this round could have caused, and must not.

    mart_json_default alone raises for anything that is not a point. These two sites promised a
    str() fallback long before points existed, and everything below reaches it today.
    """
    assert json.loads(dumps({"v": value}))["v"] == expected


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_d_a_set_still_serialises_through_str(dumps) -> None:
    restored = json.loads(dumps({"v": {1}}))

    assert restored["v"] == "{1}"


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_d_nothing_raises_where_it_used_to_fall_back(dumps) -> None:
    payload = {
        "when": datetime.datetime(2026, 7, 28, 12, 0),
        "amount": decimal.Decimal("1.5"),
        "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "obj": _Custom(),
        "point": point(FULL),
    }

    restored = json.loads(dumps(payload))

    assert restored["point"] == FULL
    assert restored["when"] == "2026-07-28 12:00:00"
    assert restored["amount"] == "1.5"


# ------------------------------------------------- 계열 e: ★ legacy rows stay readable


LEGACY_TRACE_JSON = (
    '{"tools_called":["get_brand_metric"],'
    '"latest_point":"MartJsonPoint(raw_value=80.39, ms=3.76, rank=6)",'
    '"conversation_slots":{"brand":"리바로"}}'
)
LEGACY_WITH_ADDRESS = (
    '{"latest_point":"MartJsonPoint(raw_value=80.39, ms=<..._Missing object at 0x10b057620>)"}'
)


def test_e_a_row_written_before_this_change_is_still_readable() -> None:
    """★ Old rows exist and must not be rewritten. Reading them must keep working."""
    restored = ch._json_object(LEGACY_TRACE_JSON)

    assert restored["tools_called"] == ["get_brand_metric"]
    assert restored["conversation_slots"] == {"brand": "리바로"}
    assert isinstance(restored["latest_point"], str)


def test_e_history_projection_also_reads_the_old_shape() -> None:
    restored = hp._json_loads(LEGACY_TRACE_JSON)

    assert restored["tools_called"] == ["get_brand_metric"]


def test_e_even_the_address_leaking_shape_still_reads() -> None:
    """Rows written before JSER fixed the repr contain a memory address. Still just a string."""
    restored = ch._json_object(LEGACY_WITH_ADDRESS)

    assert isinstance(restored["latest_point"], str)
    assert "0x" in restored["latest_point"]


def test_e_no_backfill_happens_on_read() -> None:
    """Reading must not try to reconstruct the lost keys; that would invent data."""
    restored = ch._json_object(LEGACY_TRACE_JSON)

    assert not isinstance(restored["latest_point"], dict)


# ------------------------------------------------- 계열 f: None, empty, nesting, lists


@pytest.mark.parametrize("dumps", SERIALIZERS)
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"p": None}, {"p": None}),
        ({"p": {}}, {"p": {}}),
        ({"p": []}, {"p": []}),
        ({"p": {"raw_value": None}}, {"p": {"raw_value": None}}),
    ],
    ids=["none", "empty-dict", "empty-list", "null-raw-value"],
)
def test_f_empty_and_null_shapes_are_unchanged(dumps, payload, expected) -> None:
    assert json.loads(dumps(payload)) == expected


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_f_points_inside_a_list_are_all_converted(dumps) -> None:
    restored = json.loads(dumps({"series": [point({"raw_value": v}) for v in (1.0, 2.0, 3.0)]}))

    assert restored["series"] == [{"raw_value": 1.0}, {"raw_value": 2.0}, {"raw_value": 3.0}]


@pytest.mark.parametrize("dumps", SERIALIZERS)
def test_f_deeply_nested_points_are_converted(dumps) -> None:
    restored = json.loads(dumps({"a": {"b": {"c": [{"d": point(FULL)}]}}}))

    assert restored["a"]["b"]["c"][0]["d"] == FULL


# ------------------------------------------------- 계열 g: all five columns


@pytest.mark.parametrize("dumps", SERIALIZERS)
@pytest.mark.parametrize("column", sorted(RAW_COLUMNS))
def test_g_each_mart_column_persists_byte_identically(dumps, column: str) -> None:
    raw = RAW_COLUMNS[column]

    written = dumps(compact_mart_json(raw, column=column))

    assert written == json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------- the guard itself


def test_the_helper_converts_a_point_and_falls_back_for_everything_else() -> None:
    assert mart_json_default_or_str(point(FULL)) == FULL
    assert mart_json_default_or_str(decimal.Decimal("1.5")) == "1.5"
    assert mart_json_default_or_str(_Custom()) == "custom-str"


def test_the_helper_never_raises() -> None:
    """That is the difference from mart_json_default, and the reason it exists."""
    assert mart_json_default_or_str(object()).startswith("<object object")


# ------------------------------------------------- memory must be untouched


def test_the_points_keep_slots_and_no_instance_dict() -> None:
    for candidate in (point({"raw_value": 1.0}), point(FULL)):
        assert hasattr(type(candidate), "__slots__")
        assert not hasattr(candidate, "__dict__")


def test_object_sizes_are_unchanged() -> None:
    assert sys.getsizeof(point({"raw_value": 1.0})) == 40
    assert sys.getsizeof(point(FULL)) == 128


def test_neither_class_gained_a_field() -> None:
    assert RawValuePoint.__slots__ == ("raw_value",)
    assert len(MartJsonPoint.__slots__) == 12
