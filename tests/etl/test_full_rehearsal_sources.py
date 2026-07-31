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


def test_iqvia_full_source_dir_passes_only_pinned_nsa_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "raw-iqvia"
    nsa = source / "NSA"
    chso = source / "CHSO"
    nsa.mkdir(parents=True)
    chso.mkdir()
    canonical = nsa / "KOR_NSA_Jun-25-2026.xlsx"
    canonical.write_bytes(b"nsa")
    (chso / "CHSO_KOR_SellOut_Basic_Feb-19-2026.xlsx").write_bytes(b"chso")
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
    assert captured["files"] == [canonical.resolve()]
    assert captured["nsa_files"] == [canonical.resolve()]
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
    compute_kwargs: list[dict[str, object]] = []

    def record_compute(
        source: str, **kwargs: object
    ) -> tuple[list[object], list[object], dict[str, object]]:
        computed.append(source)
        compute_kwargs.append(kwargs)
        return [], [], {
            "source": source,
            "brand_rows": 1,
            "market_rows": 1,
            "measures": {},
        }

    monkeypatch.setattr(s4_mart, "_ensure_isolated_schema", lambda *_args: None)
    monkeypatch.setattr(s4_mart, "_configure_mart_env", lambda *_args: None)
    monkeypatch.setattr(
        "pipeline.etl.io.mart.layer3_compute_general_v3.compute_general",
        record_compute,
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
    assert compute_kwargs[0]["commit_each_batch"] is True


def test_s4_isolated_schema_seeds_untouched_sources_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[tuple[str, object]] = []
    fetches = iter(
        (
            {"max_id": 2},
            {"batch_size": 2, "batch_last_id": 2},
            {"max_id": 0},
        )
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=None):  # type: ignore[no-untyped-def]
            statements.append((sql, params))
            if sql.startswith("INSERT INTO"):
                return 2
            return 0

        def fetchone(self):
            return next(fetches)

    class Connection:
        def __init__(self) -> None:
            self.commits = 0

        def cursor(self):
            return Cursor()

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            return None

    conn = Connection()
    monkeypatch.setattr(s4_mart, "_env", lambda: {})
    monkeypatch.setattr(s4_mart, "_admin_connect", lambda _env: conn)

    s4_mart._ensure_isolated_schema("build_db", "source_db")

    sql = "\n".join(statement for statement, _params in statements)
    assert (
        "CREATE TABLE `build_db`.`mart_general_brand_metric` "
        "LIKE `source_db`.`mart_general_brand_metric`"
    ) in sql
    assert (
        "INSERT INTO `build_db`.`mart_general_brand_metric` "
        "SELECT * FROM `source_db`.`mart_general_brand_metric`"
    ) in sql
    assert (
        "CREATE TABLE `build_db`.`mart_general_market_metric` "
        "LIKE `source_db`.`mart_general_market_metric`"
    ) in sql
    assert conn.commits == 1


def test_s4_isolated_schema_does_not_depend_on_immediate_target_max_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[tuple[str, object]] = []

    class Cursor:
        last_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=None):  # type: ignore[no-untyped-def]
            self.last_sql = sql
            statements.append((sql, params))
            if sql.startswith("INSERT INTO"):
                return 500
            return 0

        def fetchone(self):
            if "mart_general_market_metric" in self.last_sql:
                return {"max_id": 0}
            if "AS baseline_batch" in self.last_sql:
                return {"batch_size": 500, "batch_last_id": 500}
            if "FROM `source_db`.`mart_general_brand_metric`" in self.last_sql:
                return {"max_id": 500}
            if "FROM `build_db`.`mart_general_brand_metric`" in self.last_sql:
                return {"max_id": 0}
            raise AssertionError(self.last_sql)

    class Connection:
        def __init__(self) -> None:
            self.commits = 0

        def cursor(self):
            return Cursor()

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            return None

    conn = Connection()
    monkeypatch.setattr(s4_mart, "_env", lambda: {})
    monkeypatch.setattr(s4_mart, "_admin_connect", lambda _env: conn)

    s4_mart._ensure_isolated_schema("build_db", "source_db")

    assert conn.commits == 1
    assert not any(
        "MAX(`id`)" in sql
        and "FROM `build_db`.`mart_general_brand_metric`" in sql
        for sql, _params in statements
    )


def test_s4_isolated_schema_fails_closed_when_source_batch_is_not_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetches = iter(
        (
            {"max_id": 2},
            {"batch_size": 2, "batch_last_id": 2},
        )
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=None):  # type: ignore[no-untyped-def]
            if sql.startswith("INSERT INTO"):
                return 0
            return 0

        def fetchone(self):
            return next(fetches)

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self) -> None:
            raise AssertionError("incomplete copy must not commit")

        def close(self) -> None:
            return None

    monkeypatch.setattr(s4_mart, "_env", lambda: {})
    monkeypatch.setattr(s4_mart, "_admin_connect", lambda _env: Connection())

    with pytest.raises(
        RuntimeError,
        match=r"isolated baseline copy was incomplete.*copied 0 of 2",
    ):
        s4_mart._ensure_isolated_schema("build_db", "source_db")
