from __future__ import annotations

import json

from pymysql.err import OperationalError

from pipeline.scripts.api.dynamic_market.analysis_level_block_replay import (
    AnalysisLevelBlockKey,
    load_analysis_level_block,
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
        "analysis-level-block-v2-profile",
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
