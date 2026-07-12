from __future__ import annotations

import hashlib
import struct

from pipeline.scripts.etl.build_analysis_level_blocks import (
    BlockKey,
    BlockPayload,
    batch_blocks,
    framed_payload_sha256,
    current_keys,
    sharded_keys,
    profile_signature,
    variant_keys,
    _general_data,
)
from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
)


def test_schema_version_is_independent_from_app_version() -> None:
    assert ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION == "analysis-level-block-v2-profile"


def test_framed_payload_sha256_uses_unambiguous_lengths() -> None:
    left = b'{"a":"bc"}'
    right = b'{"d":1}'

    expected = hashlib.sha256(
        struct.pack(">Q", len(left)) + left + struct.pack(">Q", len(right)) + right
    ).hexdigest()

    assert framed_payload_sha256(left, right) == expected


def test_batch_blocks_stops_at_row_or_byte_limit() -> None:
    blocks = [
        BlockPayload.for_test(market_id=str(index), payload_size=size)
        for index, size in enumerate((3, 3, 3, 3, 9))
    ]

    batches = list(batch_blocks(blocks, max_rows=3, max_bytes=10))

    assert [[item.market_id for item in batch] for batch in batches] == [
        ["0", "1", "2"],
        ["3"],
        ["4"],
    ]


def test_block_payload_rejects_pre_alias_strategic_class() -> None:
    levels = {
        "data": {
            "Class": {"segments": [{"name": "Class 1"}]},
            "Class 2": {"segments": [{"name": "Class 2"}]},
        }
    }

    try:
        BlockPayload.from_sections(
            view="strategic_ml",
            market_id="ml_011",
            source="UBIST",
            measure="sales",
            analysis_levels=levels,
            market_status={},
            source_epoch="e" * 64,
            build_version="test",
        )
    except ValueError as exc:
        assert "post-alias" in str(exc)
    else:
        raise AssertionError("pre-alias ml_011 payload must be rejected")


def test_profile_signature_sorts_channels_before_hashing() -> None:
    assert profile_signature(["전체", "의원", "병원"]) == profile_signature(["병원", "전체", "의원"])


def test_variant_keys_add_profile_and_trim_dimensions() -> None:
    base = [
        BlockKey("general", "A10C1", "UBIST", "sales"),
        BlockKey("general", "A10C1", "IQVIA", "sales"),
        BlockKey("strategic_ml", "ml_001", "UBIST", "sales"),
        BlockKey("strategic_cd", "cd_001", "IQVIA", "sales"),
    ]

    expanded = variant_keys(
        base,
        general_profiles={("A10C1", "sales"): [("fallback", "brand-a"), ("target", "brand-b")]},
    )

    assert expanded == [
        BlockKey("general", "A10C1", "UBIST", "sales", "fallback", "full", "brand-a"),
        BlockKey("general", "A10C1", "UBIST", "sales", "target", "full", "brand-b"),
        BlockKey("general", "A10C1", "IQVIA", "sales", "", "full", None),
        BlockKey("strategic_ml", "ml_001", "UBIST", "sales", "", "full", None),
        BlockKey("strategic_ml", "ml_001", "UBIST", "sales", "", "trim", None),
        BlockKey("strategic_cd", "cd_001", "IQVIA", "sales", "", "full", None),
        BlockKey("strategic_cd", "cd_001", "IQVIA", "sales", "", "trim", None),
    ]


def test_general_data_places_focus_in_filters(monkeypatch) -> None:
    captured = {}

    def fake_build(request):
        captured["request"] = request
        return {"result": {"data": {}}}

    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks._build_general_dynamic_response",
        fake_build,
    )

    _general_data(market_id="A10C1", source="ubist", measure="sales", focus_brand_key="brand-a")

    assert captured["request"].filters.focus_brand_key == "brand-a"

def test_sharded_keys_partition_all_keys(monkeypatch) -> None:
    keys = [BlockKey("general", str(index), "UBIST", "sales") for index in range(3131)]
    partitions = []
    for index in range(4):
        monkeypatch.setenv("MALB_SHARD_COUNT", "4")
        monkeypatch.setenv("MALB_SHARD_INDEX", str(index))
        partitions.extend(sharded_keys(keys))

    assert sorted(item.market_id for item in partitions) == sorted(item.market_id for item in keys)
    assert len({item.market_id for item in partitions}) == 3131


def test_current_keys_is_scoped_to_epoch_and_build(monkeypatch) -> None:
    captured = {}

    def fake_fetch_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"view": "general", "market_id": "A10N1", "source": "UBIST", "measure": "sales", "profile_sig": "abc", "trim_mode": "full"}]

    monkeypatch.setattr("pipeline.scripts.etl.build_analysis_level_blocks.db.fetch_all", fake_fetch_all)

    assert current_keys(source_epoch="epoch", build_version="build") == {
        BlockKey("general", "A10N1", "UBIST", "sales", "abc", "full", None)
    }
    assert captured["params"] == ("epoch", "build")
    assert "source_epoch = %s" in captured["sql"]
    assert "profile_sig" in captured["sql"]
