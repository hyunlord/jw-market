from __future__ import annotations

import os
from types import SimpleNamespace

from pipeline.scripts.ingest_hook import downstream_refresh, job_runner
from pipeline.scripts.ingest_hook.category_map import resolve_category


def test_ubist_refresh_runs_orchestrator_then_only_normal_caches(monkeypatch):
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        downstream_refresh,
        "_run",
        lambda argv: seen.append(argv),
    )

    downstream_refresh.run(resolve_category("ubist"))

    assert "pipeline.orchestrator" in seen[0]
    assert seen[1][-1] == "pipeline.scripts.etl.build_cache_brands"
    assert seen[2][-1] == "pipeline.scripts.etl.build_cache_market_status"
    assert all("cache_cause" not in " ".join(argv) for argv in seen)
    assert all("cache_deep_analysis" not in " ".join(argv) for argv in seen)


def test_nsa_and_mi_master_refresh_normal_caches(monkeypatch):
    for category in ("iqvia_nsa", "mi_master"):
        seen: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            downstream_refresh,
            "_run",
            lambda argv, output=seen: output.append(argv),
        )

        downstream_refresh.run(resolve_category(category))

        assert len(seen) == 3
        assert "pipeline.orchestrator" in seen[0]
        assert seen[1][-1] == "pipeline.scripts.etl.build_cache_brands"
        assert seen[2][-1] == "pipeline.scripts.etl.build_cache_market_status"


def test_csd_channel_direct_serving_needs_no_unrelated_cache_rebuild(monkeypatch):
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(downstream_refresh, "_run", lambda argv: seen.append(argv))

    downstream_refresh.run(resolve_category("iqvia_csd_channel"))

    assert seen == []


def test_keyword_refresh_regenerates_topic_axis_without_legacy_caches(monkeypatch):
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(downstream_refresh, "_run", lambda argv: seen.append(argv))

    downstream_refresh.run(resolve_category("iqvia_csd_keyword"))

    assert len(seen) == 1
    assert "pipeline.scripts.etl.brand_activity.brand_activity_replay" in seen[0]
    assert "--only" in seen[0]
    assert "topic" in seen[0]
    assert "--execute" in seen[0]
    assert "--save-to-db" in seen[0]
    assert "cache_cause" not in " ".join(seen[0])
    assert "cache_deep_analysis" not in " ".join(seen[0])


def test_subprocess_failure_is_fail_closed(monkeypatch):
    class Result:
        returncode = 9

    monkeypatch.setattr(downstream_refresh.subprocess, "run", lambda *_a, **_kw: Result())

    try:
        downstream_refresh.run(resolve_category("ubist"))
    except downstream_refresh.DownstreamRefreshError as exc:
        assert "rc=9" in str(exc)
    else:
        raise AssertionError("refresh failure did not fail closed")


def test_refresh_commands_use_declared_production_database(monkeypatch):
    captured = {}
    monkeypatch.setenv("MARIADB_DATABASE", "wrong_db")
    monkeypatch.setenv("INGEST_LOAD_PRODUCTION_DB", "jw_mart_test2")

    def fake_run(argv, *, check, env):
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(downstream_refresh.subprocess, "run", fake_run)

    downstream_refresh._run(("python3", "-m", "example"))

    assert captured["env"]["MARIADB_DATABASE"] == "jw_mart_test2"
    assert captured["env"]["DB_NAME"] == "jw_mart_test2"


def test_job_runner_refresh_uses_production_db_override(monkeypatch):
    spec = resolve_category("iqvia_nsa")
    seen: list[tuple[str | None, str | None]] = []
    monkeypatch.setenv("INGEST_LOAD_PRODUCTION_DB", "serving_target")
    monkeypatch.setenv("MARIADB_DATABASE", "staging_source")
    monkeypatch.setenv("DB_NAME", "staging_source")
    monkeypatch.setattr(
        job_runner,
        "_run_commands",
        lambda _label, _argv: seen.append(
            (os.environ.get("MARIADB_DATABASE"), os.environ.get("DB_NAME"))
        ),
    )

    job_runner._refresh_category(spec)

    assert seen
    assert set(seen) == {("serving_target", "serving_target")}
    assert os.environ["MARIADB_DATABASE"] == "staging_source"
    assert os.environ["DB_NAME"] == "staging_source"
