from __future__ import annotations

from pathlib import Path

from pipeline.scripts.etl.brand_activity import brand_activity_replay as replay


def test_replay_from_stage_runs_stage_master_topic_in_order(monkeypatch, tmp_path: Path) -> None:
    """Given a stage start, When replay runs dry, Then later stages are planned in order without writes."""
    calls: list[tuple[replay.Stage, bool, bool]] = []

    def fake_stage(options: replay.ReplayOptions) -> dict[str, replay.JsonValue]:
        calls.append((replay.Stage.STAGE, options.execute, options.save_to_db))
        return {"stage": "stage", "execute": options.execute}

    def fake_master(options: replay.ReplayOptions) -> dict[str, replay.JsonValue]:
        calls.append((replay.Stage.MASTER, options.execute, options.save_to_db))
        return {"stage": "master", "execute": options.execute}

    def fake_topic(options: replay.ReplayOptions) -> dict[str, replay.JsonValue]:
        calls.append((replay.Stage.TOPIC, options.execute, options.save_to_db))
        return {"stage": "topic", "execute": options.execute}

    monkeypatch.setattr(replay, "_run_stage", fake_stage)
    monkeypatch.setattr(replay, "_run_master", fake_master)
    monkeypatch.setattr(replay, "_run_topic", fake_topic)

    options = replay.ReplayOptions(
        start=replay.Stage.STAGE,
        only=None,
        execute=False,
        save_to_db=False,
        raw_source=tmp_path / "raw",
        legacy_raw_source=tmp_path / "legacy",
        xlsx=tmp_path / "master.xlsx",
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
        window=None,
        audit_dir=tmp_path / "audit",
        topic=replay.TopicOptions(max_real_calls=3),
    )

    result = replay.replay(options)

    assert result["plan"] == ["stage", "master", "topic"]
    assert calls == [
        (replay.Stage.STAGE, False, False),
        (replay.Stage.MASTER, False, False),
        (replay.Stage.TOPIC, False, False),
    ]


def test_replay_only_raw_does_not_run_later_stages(monkeypatch, tmp_path: Path) -> None:
    """Given only raw, When replay executes, Then no later stage function is reached."""
    calls: list[replay.Stage] = []

    def fake_raw(options: replay.ReplayOptions) -> dict[str, replay.JsonValue]:
        calls.append(replay.Stage.RAW)
        return {"stage": "raw", "execute": options.execute}

    def fail_later(_options: replay.ReplayOptions) -> dict[str, replay.JsonValue]:
        raise AssertionError("later stage should not run")

    monkeypatch.setattr(replay, "_run_raw", fake_raw)
    monkeypatch.setattr(replay, "_run_stage", fail_later)
    monkeypatch.setattr(replay, "_run_master", fail_later)
    monkeypatch.setattr(replay, "_run_topic", fail_later)

    options = replay.ReplayOptions(
        start=replay.Stage.RAW,
        only=replay.Stage.RAW,
        execute=True,
        save_to_db=False,
        raw_source=tmp_path / "raw",
        legacy_raw_source=tmp_path / "legacy",
        xlsx=tmp_path / "master.xlsx",
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
        window=("2024-01", "2026-12"),
        audit_dir=tmp_path / "audit",
        topic=replay.TopicOptions(max_real_calls=1),
    )

    result = replay.replay(options)

    assert result["plan"] == ["raw"]
    assert calls == [replay.Stage.RAW]
