from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.etl.stages import s1_load, s4_mart, s5_mart


def test_ubist_full_source_dir_passes_every_workbook_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "raw-ubist"
    source.mkdir()
    first = source / "a.xlsx"
    second = source / "nested" / "b.xlsx"
    second.parent.mkdir()
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    (source / "~$lock.xlsx").write_bytes(b"lock")
    captured: dict[str, object] = {}

    def fake_load(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(s1_load, "run_ubist_load", fake_load)
    rc = s1_load.run(
        {
            "source": "ubist",
            "ubist_source_dir": str(source),
            "target_dir": str(tmp_path / "parquet"),
            "ubist_mode": "replace",
        }
    )

    assert rc == 0
    assert captured["paths"] == [first.resolve(), second.resolve()]
    assert captured["truncate"] is True


def test_iqvia_full_source_dir_passes_every_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "raw-iqvia"
    source.mkdir()
    expected = [source / "a.csv", source / "b.xlsx"]
    for path in expected:
        path.write_bytes(b"source")
    (source / "ignore.txt").write_text("ignore", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(s1_load.iqvia_loader, "init_target_schema", lambda target, source: None)

    def fake_materialize(files, target, **kwargs):  # type: ignore[no-untyped-def]
        captured["files"] = files
        return {"2026-Q2": 1}

    monkeypatch.setattr(s1_load.iqvia_loader, "materialize_record_parquet", fake_materialize)
    monkeypatch.setattr(
        s1_load.iqvia_loader,
        "materialize_iqvia_nsa_parquet",
        lambda files, target: captured.update({"nsa_files": files, "nsa_target": target}) or {"2026-Q2": 1},
    )
    monkeypatch.setattr(s1_load.iqvia_loader, "load_record_parquet_source", lambda *a, **k: 2)
    nsa_target = tmp_path / "iqvia-nsa"
    rc = s1_load.run(
        {
            "source": "iqvia",
            "iqvia_source_dir": str(source),
            "iqvia_nsa_dir": str(nsa_target),
            "target_db": "jw_mart_rehearsal_r1",
            "source_db": "jw_mart_d2_stage_20260630_r2",
        }
    )

    assert rc == 0
    assert captured["files"] == [path.resolve() for path in expected]
    assert captured["nsa_files"] == [path.resolve() for path in expected]
    assert captured["nsa_target"] == nsa_target


@pytest.mark.parametrize("stage", [s4_mart, s5_mart])
def test_mart_stage_allows_same_rehearsal_schema(stage) -> None:  # type: ignore[no-untyped-def]
    stage._validate_schema_pair("jw_mart_rehearsal_r1", "jw_mart_rehearsal_r1")


@pytest.mark.parametrize("stage", [s4_mart, s5_mart])
@pytest.mark.parametrize("database", ["jw_mart", "jw_mart_d2_stage_20260630_r2", "other"])
def test_mart_stage_rejects_same_nonrehearsal_schema(stage, database: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        stage._validate_schema_pair(database, database)


def test_s4_mart_limits_compute_to_requested_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    computed: list[str] = []

    monkeypatch.setattr(s4_mart, "_ensure_isolated_schema", lambda *_args: None)
    monkeypatch.setattr(s4_mart, "_configure_mart_env", lambda *_args: None)
    monkeypatch.setattr(
        "pipeline.etl.io.mart.layer3_compute_general_v3.compute_general",
        lambda source, **_kwargs: computed.append(source)
        or ([], [], {"source": source, "brand_rows": 1, "market_rows": 1, "measures": {}}),
    )

    rc = s4_mart.run(
        {
            "target_db": "jw_mart_ingest_shadow_build_run1",
            "source_db": "jw_mart",
            "sources": ("ubist",),
        }
    )

    assert rc == 0
    assert computed == ["ubist"]
