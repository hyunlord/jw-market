from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from pipeline.scripts.deploy import strategic_stage_import as stage_import
from pipeline.scripts.deploy.strategic_reload_publish import STRATEGIC_RELOAD_TABLES


def test_stage_import_tables_are_exact_reload_body_tables() -> None:
    assert stage_import.STAGE_IMPORT_TABLES == STRATEGIC_RELOAD_TABLES


def test_guard_stage_import_rejects_live_and_existing_staging() -> None:
    for target_db in ("jw_mart", "jw_mart_test_stage2", "jw_mart_d1_stage_20260625_173115", "scratch"):
        try:
            stage_import.guard_stage_import(source_db="jw_mart", target_db=target_db)
        except ValueError as exc:
            assert target_db in str(exc)
        else:
            raise AssertionError(f"expected {target_db} to be rejected")


def test_guard_stage_import_accepts_new_stage_repro_schema() -> None:
    stage_import.guard_stage_import(source_db="jw_mart", target_db="jw_mart_d1_stage_repro_20260626_010203")


def test_dump_only_uses_source_env_and_writes_manifest(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    dump_path = tmp_path / "stage.sql.gz"
    manifest_path = tmp_path / "stage.manifest.json"
    source_env = tmp_path / "source.env"
    source_env.write_text("MARIADB_HOST=source-db\nMARIADB_PASSWORD=placeholder-value\n", encoding="utf-8")

    def fake_dump_tables(*, target_db: str, tables: tuple[str, ...], dump_path: Path) -> object:
        calls.append(("dump", target_db, tables, stage_import.os.environ.get("MARIADB_HOST")))
        dump_path.write_bytes(b"dump")
        return stage_import.DumpResult(dump_path, dump_path.stat().st_size, 1.25)

    class Conn:
        def close(self) -> None:
            calls.append(("close", "source"))

    def fake_connect_admin() -> Conn:
        calls.append(("connect", stage_import.os.environ.get("MARIADB_HOST")))
        return Conn()

    def fake_capture_manifest(
        conn: object,
        *,
        run_id: str,
        source_db: str,
        build_db: str,
        tables: tuple[str, ...],
    ) -> dict[str, object]:
        calls.append(("manifest", run_id, source_db, build_db, tables))
        return {"run_id": run_id, "source_db": source_db, "build_db": build_db, "tables": []}

    monkeypatch.setattr(stage_import, "dump_tables", fake_dump_tables)
    monkeypatch.setattr(stage_import, "connect_admin", fake_connect_admin)
    monkeypatch.setattr(stage_import, "capture_manifest", fake_capture_manifest)
    monkeypatch.setattr(stage_import, "attach_dump_to_manifest", lambda manifest, dump_path, dump_seconds: {**manifest, "dump": {"path": str(dump_path)}})
    monkeypatch.setattr(stage_import, "db_endpoint_summary", lambda: {"host": stage_import.os.environ.get("MARIADB_HOST", "")})

    summary = stage_import.run_dump_only(
        stage_import.DumpOnlyConfig(
            source_db="jw_mart",
            run_id="run1",
            dump_path=dump_path,
            manifest_path=manifest_path,
            source_env_file=source_env,
        )
    )

    assert summary["mode"] == "dump_only"
    assert ("dump", "jw_mart", STRATEGIC_RELOAD_TABLES, "source-db") in calls
    assert ("connect", "source-db") in calls
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["policy"]["target_prefix"] == "jw_mart_d1_stage_"


def test_import_from_dump_uses_target_env_and_verifies_manifest(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    dump_path = tmp_path / "stage.sql.gz"
    dump_path.write_bytes(b"dump")
    manifest_path = tmp_path / "stage.manifest.json"
    manifest_path.write_text(
        json.dumps({"run_id": "run1", "source_db": "jw_mart", "build_db": "jw_mart", "tables": []}),
        encoding="utf-8",
    )
    target_env = tmp_path / "target.env"
    target_env.write_text("MARIADB_HOST=target-db\nMARIADB_PASSWORD=placeholder-value\n", encoding="utf-8")

    class Conn:
        def close(self) -> None:
            calls.append(("close", "target"))

    def fake_connect_admin() -> Conn:
        calls.append(("connect", stage_import.os.environ.get("MARIADB_HOST")))
        return Conn()

    monkeypatch.setattr(stage_import, "connect_admin", fake_connect_admin)
    monkeypatch.setattr(stage_import, "schema_exists", lambda conn, db_name: False)
    monkeypatch.setattr(stage_import, "ensure_schema_exists", lambda conn, db_name, create: calls.append(("ensure", db_name, create)))
    monkeypatch.setattr(stage_import, "ensure_direct_import_target_absent", lambda conn, target_db, tables: calls.append(("absent", target_db, tables)))
    monkeypatch.setattr(stage_import, "restore_dump_into_schema", lambda target_db, dump_path: stage_import.RestoreResult(dump_path, dump_path.stat().st_size, 2.5))
    monkeypatch.setattr(stage_import, "verify_against_manifest", lambda conn, target_db, manifest: [{"table": "cache_cause", "row_count": 19360}])
    monkeypatch.setattr(stage_import, "db_endpoint_summary", lambda: {"host": stage_import.os.environ.get("MARIADB_HOST", "")})

    summary = stage_import.run_import_from_dump(
        stage_import.ImportConfig(
            target_db="jw_mart_d1_stage_repro_20260626_010203",
            dump_path=dump_path,
            manifest_path=manifest_path,
            output_manifest_path=tmp_path / "imported.manifest.json",
            target_env_file=target_env,
        )
    )

    assert summary["mode"] == "import_from_dump"
    assert ("connect", "target-db") in calls
    assert ("ensure", "jw_mart_d1_stage_repro_20260626_010203", True) in calls
    assert ("absent", "jw_mart_d1_stage_repro_20260626_010203", STRATEGIC_RELOAD_TABLES) in calls
    assert summary["verification"] == [{"table": "cache_cause", "row_count": 19360}]


def test_manifest_only_attaches_existing_dump_without_redumping(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    dump_path = tmp_path / "stage.sql.gz"
    dump_path.write_bytes(b"dump")
    manifest_path = tmp_path / "stage.manifest.json"

    class Conn:
        def close(self) -> None:
            calls.append(("close", "source"))

    monkeypatch.setattr(stage_import, "connect_admin", lambda: Conn())
    monkeypatch.setattr(
        stage_import,
        "capture_manifest",
        lambda conn, run_id, source_db, build_db, tables: {
            "run_id": run_id,
            "source_db": source_db,
            "build_db": build_db,
            "tables": [],
        },
    )
    monkeypatch.setattr(stage_import, "db_endpoint_summary", lambda: {"host": "source-db"})

    summary = stage_import.run_manifest_only(
        stage_import.ManifestOnlyConfig(
            source_db="jw_mart",
            run_id="run1",
            dump_path=dump_path,
            manifest_path=manifest_path,
        )
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert summary["mode"] == "manifest_only"
    assert payload["dump"]["path"] == str(dump_path)
    assert payload["policy"]["mode"] == "manifest_only"


def test_dump_import_writes_import_manifest_next_to_source_manifest(tmp_path, monkeypatch) -> None:
    dump_path = tmp_path / "stage.sql.gz"
    manifest_path = tmp_path / "stage.manifest.json"
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(stage_import, "guard_stage_import", lambda source_db, target_db: calls.append(("guard", source_db, target_db)))
    monkeypatch.setattr(
        stage_import,
        "run_dump_only",
        lambda config: calls.append(("dump", config.manifest_path)) or {"mode": "dump_only"},
    )

    def fake_import(config: stage_import.ImportConfig) -> dict[str, object]:
        calls.append(("import_manifest", config.output_manifest_path))
        return {"mode": "import_from_dump"}

    monkeypatch.setattr(stage_import, "run_import_from_dump", fake_import)

    stage_import.run_dump_import(
        stage_import.DumpImportConfig(
            source_db="jw_mart",
            target_db="jw_mart_d1_stage_repro_20260626_010203",
            run_id="run1",
            dump_path=dump_path,
            manifest_path=manifest_path,
        )
    )

    assert ("dump", manifest_path) in calls
    assert ("import_manifest", tmp_path / "stage.manifest.imported.json") in calls


def test_import_from_dump_rejects_existing_schema(tmp_path, monkeypatch) -> None:
    dump_path = tmp_path / "stage.sql.gz"
    dump_path.write_bytes(b"dump")
    manifest_path = tmp_path / "stage.manifest.json"
    manifest_path.write_text(json.dumps({"tables": []}), encoding="utf-8")

    class Conn:
        def close(self) -> None:
            return None

    monkeypatch.setattr(stage_import, "connect_admin", lambda: Conn())
    monkeypatch.setattr(stage_import, "schema_exists", lambda conn, db_name: True)

    try:
        stage_import.run_import_from_dump(
            stage_import.ImportConfig(
                target_db="jw_mart_d1_stage_repro_20260626_010203",
                dump_path=dump_path,
                manifest_path=manifest_path,
                output_manifest_path=tmp_path / "out.json",
            )
        )
    except RuntimeError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected existing schema to be rejected")


def test_compare_schema_is_read_only_and_allows_existing_staging(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    manifest_path = tmp_path / "stage.manifest.json"
    manifest_path.write_text(json.dumps({"tables": []}), encoding="utf-8")

    class Conn:
        def close(self) -> None:
            calls.append(("close", "target"))

    monkeypatch.setattr(stage_import, "connect_admin", lambda: Conn())
    monkeypatch.setattr(stage_import, "schema_exists", lambda conn, db_name: True)
    monkeypatch.setattr(stage_import, "verify_against_manifest", lambda conn, target_db, manifest: [{"table": "cache_cause", "row_count": 19360}])
    monkeypatch.setattr(stage_import, "db_endpoint_summary", lambda: {"host": "target-db"})

    summary = stage_import.run_compare_schema(
        stage_import.CompareConfig(
            target_db="jw_mart_d1_stage_20260625_173115",
            manifest_path=manifest_path,
            output_manifest_path=tmp_path / "compare.json",
        )
    )

    assert summary["mode"] == "compare_schema"
    assert summary["verification"] == [{"table": "cache_cause", "row_count": 19360}]


def test_restore_stream_batches_insert_lines_and_strips_dump_wrappers() -> None:
    dump = BytesIO(
        b"CREATE TABLE cache_cause (brand text);\n"
        b"BEGIN;\n"
        b"INSERT INTO cache_cause VALUES ('a');\n"
        b"INSERT INTO cache_cause VALUES ('b');\n"
        b"INSERT INTO cache_cause VALUES ('c');\n"
        b"COMMIT;\n"
        b"CREATE TABLE cache_deep_analysis (brand text);\n"
    )
    out = BytesIO()

    stage_import._copy_restore_stream_with_insert_batches(dump, out, insert_batch_size=2)

    assert out.getvalue() == (
        b"CREATE TABLE cache_cause (brand text);\n"
        b"START TRANSACTION;\n"
        b"INSERT INTO cache_cause VALUES ('a');\n"
        b"INSERT INTO cache_cause VALUES ('b');\n"
        b"COMMIT;\n"
        b"START TRANSACTION;\n"
        b"INSERT INTO cache_cause VALUES ('c');\n"
        b"COMMIT;\n"
        b"CREATE TABLE cache_deep_analysis (brand text);\n"
    )
