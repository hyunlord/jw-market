from __future__ import annotations

import json

import pytest
from pymysql.err import OperationalError

from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    analysis_level_profile_signature,
    channel_profile_signature,
)
from pipeline.scripts.api.dynamic_market.analysis_level_block_replay import (
    AnalysisLevelBlockKey,
    load_analysis_level_block,
    reset_analysis_level_replay_stats_for_test,
)
from pipeline.scripts.api.dynamic_market.general_analysis_levels import _load_precomputed_general_block
from pipeline.scripts.api.dynamic_market.types import DimensionFilter, MarketDefinition


GENERAL_ROW_FILTERS = (
    pytest.param("ubist", "seller", id="ubist-seller"),
    pytest.param("ubist", "molecule", id="ubist-molecule"),
    pytest.param("ubist", "molecule_strength", id="ubist-molecule-strength"),
    pytest.param("ubist", "form", id="ubist-form"),
    pytest.param("ubist", "route", id="ubist-route"),
    pytest.param("ubist", "reimbursement", id="ubist-reimbursement"),
    pytest.param("ubist", "atc3", id="ubist-atc3"),
    pytest.param("ubist", "atc4", id="ubist-atc4"),
    pytest.param("iqvia_nsa", "mfr", id="iqvia-mfr"),
    pytest.param("iqvia_nsa", "molecule_type", id="iqvia-molecule-type"),
    pytest.param("iqvia_nsa", "molecule_desc", id="iqvia-molecule-desc"),
    pytest.param("iqvia_nsa", "pack", id="iqvia-pack"),
    pytest.param("iqvia_nsa", "strength", id="iqvia-strength"),
    pytest.param("iqvia_nsa", "nhi", id="iqvia-nhi"),
)


def test_load_analysis_level_block_requires_six_tuple_build_and_epoch(monkeypatch) -> None:
    captured = {}
    levels = '{"levels":["Brand"],"data":{"Brand":{"segments":[]}}}'
    status = '{"levels":["Brand"],"data":{"Brand":{"segments":[]}}}'

    def fake_fetch_one(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return {
            "analysis_levels_json": levels,
            "analysis_level_market_status_json": status,
        }

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        fake_fetch_one,
    )

    key = AnalysisLevelBlockKey("general", "C10A1", "UBIST", "sales", "profile", "full")
    block = load_analysis_level_block(key=key, source_epoch="epoch")

    assert block is not None
    assert block.analysis_levels == json.loads(levels)
    assert block.analysis_level_market_status == json.loads(status)
    assert captured["params"] == (
        "general",
        "C10A1",
        "UBIST",
        "sales",
        "profile",
        "full",
        "analysis-level-block-v5-unclassified-partitions",
        "epoch",
    )
    assert "build_version = %s" in captured["sql"]
    assert "source_epoch = %s" in captured["sql"]


def test_load_analysis_level_block_falls_back_on_missing_or_invalid_rows(monkeypatch) -> None:
    key = AnalysisLevelBlockKey("strategic_ml", "ml_003", "IQVIA", "sales", "", "trim")
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        lambda *_args, **_kwargs: None,
    )
    assert load_analysis_level_block(key=key, source_epoch="epoch") is None

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        lambda *_args, **_kwargs: {
            "analysis_levels_json": "not-json",
            "analysis_level_market_status_json": "{}",
        },
    )
    assert load_analysis_level_block(key=key, source_epoch="epoch") is None


def test_load_analysis_level_block_falls_back_on_table_error(monkeypatch) -> None:
    def missing_table(*_args, **_kwargs):
        raise OperationalError(1146, "table missing")

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        missing_table,
    )

    key = AnalysisLevelBlockKey("strategic_cd", "cd_001", "UBIST", "volume", "", "full")
    assert load_analysis_level_block(key=key, source_epoch="epoch") is None


def test_stored_sections_keep_canonical_bytes(monkeypatch) -> None:
    levels = '{"levels":["Class","Brand"],"data":{"Class":{"segments":[{"name":"A","value":1.25}]},"Brand":{"segments":[]}}}'
    status = '{"channels":["전체","의원"],"data":{"Class":{"segments":[]}}}'
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        lambda *_args, **_kwargs: {
            "analysis_levels_json": levels,
            "analysis_level_market_status_json": status,
        },
    )

    key = AnalysisLevelBlockKey("general", "A10C1", "UBIST", "sales", "p", "full")
    block = load_analysis_level_block(key=key, source_epoch="epoch")

    assert block is not None
    assert json.dumps(block.analysis_levels, ensure_ascii=False, separators=(",", ":")) == levels
    assert json.dumps(block.analysis_level_market_status, ensure_ascii=False, separators=(",", ":")) == status


def test_replay_logs_hit_rate_for_hits_and_misses(monkeypatch, caplog) -> None:
    reset_analysis_level_replay_stats_for_test()
    rows = iter(
        (
            None,
            {
                "analysis_levels_json": "{}",
                "analysis_level_market_status_json": "{}",
            },
        )
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_block_replay.db.fetch_one",
        lambda *_args, **_kwargs: next(rows),
    )
    key = AnalysisLevelBlockKey("general", "C10A1", "UBIST", "sales")

    with caplog.at_level("INFO"):
        assert load_analysis_level_block(key=key, source_epoch="epoch") is None
        assert load_analysis_level_block(key=key, source_epoch="epoch") is not None

    stats = [record.message for record in caplog.records if "analysis_level_block_replay_stats" in record.message]
    assert any("hits=0 misses=1 fallbacks=0 hit_rate=0.0000" in message for message in stats)
    assert any("hits=1 misses=1 fallbacks=0 hit_rate=0.5000" in message for message in stats)


@pytest.mark.parametrize(("source", "dimension_type"), GENERAL_ROW_FILTERS)
def test_general_replay_key_never_reuses_unfiltered_block_for_row_filter(
    monkeypatch,
    source,
    dimension_type,
) -> None:
    captured: dict[str, AnalysisLevelBlockKey] = {}
    channels = ["전체", "의원"]
    definition = MarketDefinition(
        view="general",
        filter_echo={"atc4": ["C10A1"]},
        source=source,
        measure="sales",
        dimension_filters=(DimensionFilter(dimension_type, ("selected-value",)),),
    )

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.general_analysis_levels.current_analysis_level_source_epoch",
        lambda: "epoch",
    )

    def fake_load(*, key, source_epoch):
        assert source_epoch == "epoch"
        captured["key"] = key
        return None

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.general_analysis_levels.load_analysis_level_block",
        fake_load,
    )

    _load_precomputed_general_block(
        definition=definition,
        source="UBIST" if source == "ubist" else "IQVIA",
        measure="sales",
        status_channels=channels,
    )

    unfiltered_profile = channel_profile_signature(channels) if source == "ubist" else ""
    assert captured["key"].profile_sig != unfiltered_profile, (
        f"{dimension_type} request reused the unfiltered replay identity"
    )


def test_analysis_level_profile_signature_canonicalizes_filter_order() -> None:
    left = analysis_level_profile_signature(
        base_profile="base",
        dimension_filters=(
            ("seller", ("B", "A")),
            ("molecule", ("M",)),
        ),
    )
    right = analysis_level_profile_signature(
        base_profile="base",
        dimension_filters=(
            ("molecule", ("M",)),
            ("seller", ("A", "B")),
        ),
    )

    assert left == right
    assert analysis_level_profile_signature(base_profile="base", dimension_filters=()) == "base"


def test_analysis_level_profile_signature_separates_period_windows() -> None:
    unbounded = analysis_level_profile_signature(
        base_profile="base",
        dimension_filters=(),
    )
    april = analysis_level_profile_signature(
        base_profile="base",
        dimension_filters=(),
        period_range=("2026-04", "2026-04"),
    )
    may = analysis_level_profile_signature(
        base_profile="base",
        dimension_filters=(),
        period_range=("2026-05", "2026-05"),
    )

    assert unbounded == "base"
    assert april != unbounded
    assert may != unbounded
    assert april != may
