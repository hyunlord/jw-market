"""A compact point must serialise to exactly what the loader read.

DICT3 replaced the per-point dict with a Mapping subclass and cut 1.938 GiB per pod. json,
however, serialises dict and nothing else, so every point became unserialisable. Nothing broke,
because every consumer reads points with .get/.items and takes scalars out — a property of
today's callers, not a contract. The first caller to hand a point to json.dumps would get a
TypeError, and two call sites would be worse than that: history_projection and
conversation_history pass default=str, so a point there would silently become a repr string and
its ms/rank and unknown keys would disappear from the record.

These tests pin the guard: to_dict() and mart_json_default() reproduce the original object
byte-for-byte under sort_keys, unknown keys come with it, and the memory win is untouched.
"""

from __future__ import annotations

import json
import sys

import pytest

from jw_chat_agent_poc.tools.query_layer.mart_json import (
    MartJsonPoint,
    RawValuePoint,
    compact_mart_json,
    dumps_mart_json,
    mart_json_default,
)


CANONICAL = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}

#: the five mart JSON columns, in the raw shape the loader receives from MySQL
RAW_COLUMNS: dict[str, dict] = {
    "metric_history": {
        "2026-05": {"raw_value": 80.39, "ms": 3.76, "rank": 6},
        "2026-04": {"raw_value": 84.93},
    },
    "channel_data": {
        "의원": {"2026-05": {"raw_value": 41.09}},
        "종병": {"2026-05": {"raw_value": 18.47, "ms": 4.23}},
    },
    "specialty_data": {"분리되지 않은 내과": {"2026-05": {"raw_value": 30.93, "ms": 3.50}}},
    "dimension_data": {"atc4": {"C10A1": {"raw_value": 55.5}, "C10C": {"raw_value": 44.5}}},
    "by_dimension": {"C10A1": {"raw_value": 55.5, "ms": 0.555, "rank": 1}},
}

WITH_UNKNOWN = {"2026-05": {"raw_value": 80.39, "brand_new_metric": 123.456, "another": "text"}}


def point(payload: dict, *, column: str = "metric_history"):
    return compact_mart_json({"p": payload}, column=column)["p"]


# ------------------------------------------------- 계열 a: a point on its own


def test_a_bare_point_serialises_to_its_original_dict() -> None:
    original = {"raw_value": 80.39}
    assert json.dumps(point(original), default=mart_json_default, **CANONICAL) == json.dumps(
        original, **CANONICAL
    )


def test_a_multi_key_point_serialises_to_its_original_dict() -> None:
    original = {"raw_value": 80.39, "ms": 3.76, "rank": 6}
    assert json.dumps(point(original), default=mart_json_default, **CANONICAL) == json.dumps(
        original, **CANONICAL
    )


def test_a_point_exposes_to_dict_directly() -> None:
    assert point({"raw_value": 80.39}).to_dict() == {"raw_value": 80.39}
    assert point({"raw_value": 1.0, "ms": 2.0}).to_dict() == {"raw_value": 1.0, "ms": 2.0}


def test_to_dict_returns_a_plain_dict_not_a_mapping_view() -> None:
    result = point({"raw_value": 1.0, "ms": 2.0}).to_dict()
    assert type(result) is dict
    result["mutable"] = True  # a caller may own the copy


# ------------------------------------------------- 계열 b: points inside structures


@pytest.mark.parametrize(
    "build",
    [
        lambda p: {"2026-05": p},
        lambda p: [p, p],
        lambda p: {"a": {"b": [p]}},
        lambda p: {"rows": [{"point": p}]},
        lambda p: (p,),
    ],
    ids=["dict", "list", "nested", "list-of-dict", "tuple"],
)
def test_b_a_structure_holding_points_serialises(build) -> None:
    original = {"raw_value": 80.39, "ms": 3.76}
    assert json.dumps(build(point(original)), default=mart_json_default, **CANONICAL) == json.dumps(
        build(original), **CANONICAL
    )


# ------------------------------------------------- 계열 c: unknown keys survive


def test_c_unknown_keys_are_in_the_serialised_output() -> None:
    compacted = compact_mart_json(WITH_UNKNOWN, column="metric_history")
    assert json.dumps(compacted, default=mart_json_default, **CANONICAL) == json.dumps(
        WITH_UNKNOWN, **CANONICAL
    )


def test_c_unknown_keys_are_named_in_the_output() -> None:
    serialized = json.dumps(
        compact_mart_json(WITH_UNKNOWN, column="metric_history"),
        default=mart_json_default,
        **CANONICAL,
    )
    assert "brand_new_metric" in serialized
    assert "123.456" in serialized
    assert "another" in serialized


def test_c_the_extra_mapping_itself_serialises() -> None:
    """RED showed mappingproxy is unserialisable on its own, not only inside a point."""
    p = compact_mart_json(WITH_UNKNOWN, column="metric_history")["2026-05"]
    assert json.loads(json.dumps(p.extra, default=mart_json_default)) == {
        "brand_new_metric": 123.456,
        "another": "text",
    }


# ------------------------------------------------- 계열 d: None, empty, nesting


@pytest.mark.parametrize(
    "original",
    [
        {"raw_value": None},
        {"raw_value": 0},
        {"raw_value": 0.0},
        {"raw_value": ""},
        {"raw_value": False},
        {"raw_value": 80.39, "ms": None, "rank": None},
        {"raw_value": -12.5, "growth_abs": -1.0},
    ],
    ids=["none", "int-zero", "float-zero", "empty-string", "false", "explicit-nulls", "negative"],
)
def test_d_edge_values_round_trip(original) -> None:
    assert json.dumps(point(original), default=mart_json_default, **CANONICAL) == json.dumps(
        original, **CANONICAL
    )


def test_d_an_absent_key_stays_absent_rather_than_becoming_null() -> None:
    """A key the source never had must not appear as null: that would change the record."""
    serialized = json.dumps(point({"raw_value": 1.0}), default=mart_json_default, **CANONICAL)
    assert serialized == '{"raw_value":1.0}'
    assert "ms" not in serialized
    assert "null" not in serialized


# ------------------------------------------------- 계열 e: all five columns


@pytest.mark.parametrize("column", sorted(RAW_COLUMNS))
def test_e_each_mart_column_serialises_byte_identically(column: str) -> None:
    raw = RAW_COLUMNS[column]
    assert json.dumps(
        compact_mart_json(raw, column=column), default=mart_json_default, **CANONICAL
    ) == json.dumps(raw, **CANONICAL)


@pytest.mark.parametrize("column", sorted(RAW_COLUMNS))
def test_e_each_column_round_trips_back_to_equal_data(column: str) -> None:
    raw = RAW_COLUMNS[column]
    restored = json.loads(
        json.dumps(compact_mart_json(raw, column=column), default=mart_json_default, **CANONICAL)
    )
    assert restored == json.loads(json.dumps(raw, **CANONICAL))


# ------------------------------------------------- 계열 f: both directions


def test_f_without_the_hook_it_still_raises() -> None:
    """The failure this round exists for. Not fixed by accident, fixed by the hook."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps({"2026-05": point({"raw_value": 80.39})})


def test_f_with_the_hook_it_succeeds() -> None:
    assert json.dumps({"2026-05": point({"raw_value": 80.39})}, default=mart_json_default)


def test_f_the_wrapper_needs_no_keyword() -> None:
    assert dumps_mart_json({"2026-05": point({"raw_value": 80.39})}, **CANONICAL) == (
        '{"2026-05":{"raw_value":80.39}}'
    )


def test_f_the_hook_does_not_mask_an_unrelated_type() -> None:
    """A guard that swallows every unknown type would hide real bugs."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps({"x": object()}, default=mart_json_default)


# ------------------------------------------------- the default=str corruption path


def test_the_repr_no_longer_leaks_sentinels_or_addresses() -> None:
    """history_projection and conversation_history pass default=str.

    Before this round a point there stringified to a repr containing
    '<..._Missing object at 0x...>' — a memory address, different every run. The repr now lists
    only the keys that are present, so that path is at least deterministic and readable.
    """
    text = repr(point({"raw_value": 80.39, "ms": 3.76}))
    assert "_Missing" not in text
    assert "0x" not in text
    assert "raw_value=80.39" in text
    assert "ms=3.76" in text
    assert "rank" not in text


def test_default_str_output_is_deterministic() -> None:
    payload = {"2026-05": point({"raw_value": 80.39, "ms": 3.76})}
    assert json.dumps(payload, default=str) == json.dumps(payload, default=str)


def test_the_class_name_stays_visible_to_the_dict3_render_assertion() -> None:
    """DICT3 asserts 'MartJsonPoint' not in serialized as a leak detector. Keep it working."""
    assert "MartJsonPoint" in repr(point({"raw_value": 1.0, "ms": 2.0}))


# ------------------------------------------------- the memory win must be untouched


def test_the_points_keep_slots_and_have_no_instance_dict() -> None:
    for p in (point({"raw_value": 1.0}), point({"raw_value": 1.0, "ms": 2.0})):
        assert hasattr(type(p), "__slots__")
        assert not hasattr(p, "__dict__")


def test_a_one_key_point_is_still_far_smaller_than_a_dict() -> None:
    compact = point({"raw_value": 80.39})
    assert type(compact) is RawValuePoint
    assert sys.getsizeof(compact) < sys.getsizeof({"raw_value": 80.39})


def test_a_multi_key_point_is_still_smaller_than_its_dict() -> None:
    original = {"raw_value": 80.39, "ms": 3.76, "rank": 6, "mom": 0.1, "yoy": 0.2}
    compact = point(original)
    assert type(compact) is MartJsonPoint
    assert sys.getsizeof(compact) < sys.getsizeof(original)


def test_neither_class_gained_a_field() -> None:
    """to_dict/repr are methods; a new field would cost memory on every one of ~128k points."""
    assert RawValuePoint.__slots__ == ("raw_value",)
    assert "extra" in MartJsonPoint.__slots__
    assert len(MartJsonPoint.__slots__) == 12
