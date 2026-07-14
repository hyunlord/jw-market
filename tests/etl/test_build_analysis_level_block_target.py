from __future__ import annotations

from pipeline.scripts.etl import build_analysis_level_blocks as malb
from pipeline.scripts.etl.build_analysis_level_blocks import (
    BlockKey,
    BlockPayload,
    current_keys,
    run_parity,
    target_db,
    target_relation,
    target_table,
    upsert_sql,
)


def _select_staging_target(monkeypatch) -> None:
    monkeypatch.setenv("MALB_TARGET_DB", malb.config.db_name)
    monkeypatch.setenv("MALB_TARGET_TABLE", "mart_analysis_level_block_staging")


def test_target_table_requires_explicit_live_or_staging_identity(monkeypatch) -> None:
    monkeypatch.delenv("MALB_TARGET_TABLE", raising=False)
    try:
        target_table()
    except RuntimeError as exc:
        assert "MALB_TARGET_TABLE is required" in str(exc)
    else:
        raise AssertionError("MALB target table must not default to live")

    monkeypatch.setenv("MALB_TARGET_TABLE", "mart_analysis_level_block")
    assert target_table() == "mart_analysis_level_block"

    monkeypatch.setenv("MALB_TARGET_TABLE", "mart_analysis_level_block_staging")
    assert target_table() == "mart_analysis_level_block_staging"

    monkeypatch.setenv("MALB_TARGET_TABLE", "mart_analysis_level_block_old")
    try:
        target_table()
    except RuntimeError as exc:
        assert "unsupported MALB_TARGET_TABLE" in str(exc)
    else:
        raise AssertionError("unexpected MALB target table must be rejected")


def test_target_relation_requires_explicit_matching_database(monkeypatch) -> None:
    monkeypatch.setenv("MALB_TARGET_TABLE", "mart_analysis_level_block_staging")
    monkeypatch.delenv("MALB_TARGET_DB", raising=False)
    try:
        target_relation()
    except RuntimeError as exc:
        assert "MALB_TARGET_DB is required" in str(exc)
    else:
        raise AssertionError("MALB target database must be explicit")

    monkeypatch.setenv("MALB_TARGET_DB", "different_db")
    try:
        target_db()
    except RuntimeError as exc:
        assert "does not match DB_NAME" in str(exc)
    else:
        raise AssertionError("MALB target database drift must fail")

    monkeypatch.setenv("MALB_TARGET_DB", malb.config.db_name)
    assert target_relation().endswith(".`mart_analysis_level_block_staging`")


def test_malb_queries_use_selected_staging_table(monkeypatch) -> None:
    captured: list[str] = []
    _select_staging_target(monkeypatch)

    def fake_fetch_all(sql, params):
        captured.append(sql)
        return []

    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.db.fetch_all",
        fake_fetch_all,
    )

    assert current_keys(source_epoch="epoch", build_version="build") == set()
    assert "mart_analysis_level_block_staging" in captured[0]
    assert "mart_analysis_level_block_staging" in upsert_sql()


def test_run_parity_reads_selected_staging_table(monkeypatch) -> None:
    captured = {}
    key = BlockKey("general", "A10N1", "UBIST", "sales")
    payload = BlockPayload.for_test(market_id=key.market_id, payload_size=2)
    _select_staging_target(monkeypatch)
    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.enumerate_keys",
        lambda: [key],
    )
    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.sharded_keys",
        lambda keys: keys,
    )
    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.source_epoch",
        lambda: "current-epoch",
    )
    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.build_block",
        lambda _key, *, source_epoch: payload,
    )

    def fake_fetch_one(sql, params):
        captured["sql"] = sql
        return {"payload_sha256": payload.payload_sha256}

    monkeypatch.setattr(
        "pipeline.scripts.etl.build_analysis_level_blocks.db.fetch_one",
        fake_fetch_one,
    )

    run_parity()

    assert "mart_analysis_level_block_staging" in captured["sql"]
