from __future__ import annotations

import gzip
import io
from pathlib import Path
from types import SimpleNamespace

from pipeline.etl.io.mart import molecule_bridge_build
from pipeline.etl.io.mart.molecule_bridge_schema import BRIDGE_INSERT_COLUMNS
from pipeline.scripts.deploy import mart_import_ops
from pipeline.scripts.deploy import mart_load_ops
from pipeline.scripts.deploy.mart_load_verify import CanonicalDigest
from pipeline.scripts.deploy.mart_load_verify import TableDigest
from pipeline.scripts.deploy.mart_load_verify import _canonical_value, _stable_column_expression


def test_build_strategic_ml_market_rows_tolerates_missing_channel_specialty_matrix() -> None:
    rows = mart_load_ops.build_strategic_ml_market_rows(
        [
            {
                "ml_id": "ml_001",
                "brand_id": "b1",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "source": "ubist",
                "measure": "sales",
                "unit_label": "KRW",
                "metric_history": {"2025": {"raw_value": 100.0, "ms": 100.0}},
                "extended_metric_history": {"2025": {"ei_5y": 1.0, "momentum_score": 2.0}},
                "channel_data": {},
                "specialty_data": {},
                "dimension_data": {},
                "dimension_channel_data": {},
                "dimension_specialty_data": {},
                "by_dimension": {"company": "Acme"},
                "raw_value_history": {"2025": 100.0},
                "overlay_data": {},
                "payload": {},
            }
        ],
        {"ml_001": {"ml_id": "ml_001", "name": "Market One"}},
    )

    assert len(rows) == 1
    assert rows[0]["ml_id"] == "ml_001"
    assert rows[0]["market_size_series"] == {"2025": 100.0}
    assert rows[0]["target_customer_competition"]["source_type"] == "computed"


def test_payload_checksum_expression_ignores_only_computed_at() -> None:
    expression = _stable_column_expression("payload")

    assert "JSON_REMOVE" in expression
    assert "$.computed_at" in expression
    assert "payload" in expression


def test_canonical_value_rounds_float_serialization_noise() -> None:
    left = _canonical_value('{"2021-01": 77684602.7, "nested": [1.0000000001]}')
    right = _canonical_value('{"nested": [1.0], "2021-01": 77684602.69999999}')

    assert left == right


def test_canonical_reference_digest_fallback_handles_slots_digest(monkeypatch) -> None:
    from pipeline.scripts.deploy import mart_load_verify

    monkeypatch.setattr(mart_load_verify, "table_digest", lambda *args: TableDigest(row_count=3, crc_sum=5, crc_xor=7))

    digest = mart_load_verify.canonical_reference_digest(object(), "jw_mart", "unknown_table")

    assert digest.row_count == 3
    assert len(digest.sha256) == 64


def test_run_bridge_reads_from_source_db_and_writes_build_db(monkeypatch) -> None:
    calls: list[dict[str, str | Path]] = []

    def fake_build_molecule_bridge(*, source_db: str, target_db: str, catalog_root: Path) -> SimpleNamespace:
        calls.append({"source_db": source_db, "target_db": target_db, "catalog_root": catalog_root})
        return SimpleNamespace(
            source_db=source_db,
            target_db=target_db,
            inserted_rows=58_330,
            candidate_rows=60_000,
            brand_keys=10,
            molecule_norms=20,
            combo_rows=30,
        )

    monkeypatch.setattr(mart_load_ops, "build_molecule_bridge", fake_build_molecule_bridge)
    monkeypatch.setattr(mart_load_ops, "first_existing", lambda *paths: Path("/tmp/catalog"))

    mart_load_ops.run_bridge(build_db="scratch_build", source_db="jw_mart", catalog_root=None)

    assert calls == [{"source_db": "jw_mart", "target_db": "scratch_build", "catalog_root": Path("/tmp/catalog")}]


def test_bridge_insert_payloads_are_batched() -> None:
    batches: list[int] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def executemany(self, sql: str, values: list[tuple[object, ...]]) -> None:
            assert "mart_brand_molecule" in sql
            batches.append(len(values))

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    payloads = [
        {column: (idx if column.endswith("_count") or column == "is_combo_component" else f"{column}-{idx}") for column in BRIDGE_INSERT_COLUMNS}
        for idx in range(5)
    ]

    molecule_bridge_build._insert_payloads(Connection(), "scratch_build", payloads, batch_size=2)

    assert batches == [2, 2, 1]


def test_copy_table_batches_by_id(monkeypatch) -> None:
    executed: list[tuple[str, tuple[int, int] | None]] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[int, int] | None = None) -> None:
            executed.append((sql, params))

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(mart_load_ops, "_ordered_columns", lambda *args: ["id", "payload"])
    monkeypatch.setattr(mart_load_ops, "_id_bounds", lambda *args: (1, 5))

    mart_load_ops._copy_table(Connection(), "build_db", "target_db", "source_table", "target_table", batch_size=2)

    assert "CREATE TABLE" in executed[0][0]
    assert [params for _, params in executed[1:]] == [(1, 2), (3, 4), (5, 6)]
    assert all("WHERE id BETWEEN %s AND %s" in sql for sql, _ in executed[1:])


def test_copy_table_batches_no_id_by_primary_key(monkeypatch) -> None:
    executed: list[tuple[str, object]] = []
    key_batches = [
        [
            {"brand": "a", "market_id": "m1", "response_json": None},
            {"brand": "b", "market_id": "m1", "response_json": None},
        ],
        [{"brand": "c", "market_id": "m2", "response_json": None}],
        [],
    ]

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def executemany(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def fetchall(self) -> list[dict[str, str]]:
            return key_batches.pop(0)

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(mart_load_ops, "_ordered_columns", lambda *args: ["brand", "market_id", "response_json"])
    monkeypatch.setattr(mart_load_ops, "_primary_key_columns", lambda *args: ["brand", "market_id"])
    monkeypatch.setattr(mart_load_ops, "_table_row_count", lambda *args: 5)

    mart_load_ops._copy_table(Connection(), "build_db", "target_db", "source_table", "target_table", batch_size=2)

    statements = [sql for sql, _ in executed]
    params = [params for _, params in executed]
    assert "CREATE TABLE" in statements[0]
    assert statements[1].startswith("SELECT `brand`,`market_id`,`response_json`")
    assert statements[1].endswith("ORDER BY `brand`,`market_id` LIMIT 2")
    assert statements[2] == (
        "INSERT INTO `target_db`.`target_table` (`brand`,`market_id`,`response_json`) "
        "VALUES (%s,%s,%s)"
    )
    assert statements[3].endswith("WHERE (`brand` > %s) OR (`brand` = %s AND `market_id` > %s) ORDER BY `brand`,`market_id` LIMIT 2")
    assert statements[4] == (
        "INSERT INTO `target_db`.`target_table` (`brand`,`market_id`,`response_json`) "
        "VALUES (%s,%s,%s)"
    )
    assert params[2] == [("a", "m1", None), ("b", "m1", None)]
    assert params[3] == ("b", "b", "m1")
    assert params[4] == [("c", "m2", None)]


def test_copy_table_caps_no_id_batch_size_at_200(monkeypatch) -> None:
    executed: list[tuple[str, object]] = []
    key_batches = [[{"query_key": str(index)} for index in range(200)], []]

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def executemany(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def fetchall(self) -> list[dict[str, str]]:
            keys = key_batches.pop(0)
            return [{"query_key": row["query_key"], "response_json": None} for row in keys]

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(mart_load_ops, "_ordered_columns", lambda *args: ["query_key", "response_json"])
    monkeypatch.setattr(mart_load_ops, "_primary_key_columns", lambda *args: ["query_key"])
    monkeypatch.setattr(mart_load_ops, "_table_row_count", lambda *args: 450)

    mart_load_ops._copy_table(Connection(), "build_db", "target_db", "source_table", "target_table", batch_size=500)

    statements = [sql for sql, _ in executed]
    assert statements[1].endswith("ORDER BY `query_key` LIMIT 200")
    assert "INSERT INTO `target_db`.`target_table`" in statements[2]
    assert statements[3].endswith("WHERE (`query_key` > %s) ORDER BY `query_key` LIMIT 200")


def test_publish_retries_transient_partial_count_before_atomic_rename(monkeypatch) -> None:
    executed: list[str] = []
    counts = iter([14_328, 14_267, 14_328])

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            executed.append(sql)

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    def fake_table_exists(_conn: Connection, db_name: str, table_name: str) -> bool:
        return (db_name, table_name) in {
            ("build_db", "mart_strategic_ml_brand_metric"),
            ("target_db", "mart_strategic_ml_brand_metric"),
        }

    monkeypatch.setattr(mart_load_ops, "table_exists", fake_table_exists)
    monkeypatch.setattr(mart_load_ops, "_copy_table", lambda *args: None)
    monkeypatch.setattr(mart_load_ops, "_table_row_count", lambda *args: next(counts))
    monkeypatch.setattr(mart_load_ops.time, "sleep", lambda _seconds: None)

    action = mart_load_ops._publish_one(
        Connection(),
        "build_db",
        "target_db",
        "mart_strategic_ml_brand_metric",
        "f116",
    )

    assert action.mode == "atomic_rename"
    assert any(statement.startswith("RENAME TABLE") for statement in executed)


def test_direct_import_manifest_verifies_canonical_digest(monkeypatch) -> None:
    manifest = {
        "tables": [
            {
                "table": "mart_general_brand_metric",
                "row_count": 2,
                "canonical_sha256": "abc123",
                "groups": {"ubist|sales": 2},
            }
        ]
    }

    monkeypatch.setattr(mart_import_ops, "table_exists", lambda *args: True)
    monkeypatch.setattr(
        mart_import_ops,
        "canonical_reference_digest",
        lambda *args: CanonicalDigest(row_count=2, sha256="abc123"),
    )
    monkeypatch.setattr(mart_import_ops, "fetch_group_counts", lambda *args: {("ubist", "sales"): 2})

    results = mart_import_ops.verify_against_manifest(object(), target_db="target_db", manifest=manifest)

    assert results == [
        {
            "table": "mart_general_brand_metric",
            "row_count": 2,
            "canonical_sha256": "abc123",
            "groups": {"ubist|sales": 2},
        }
    ]


def test_direct_import_manifest_rejects_digest_mismatch(monkeypatch) -> None:
    manifest = {
        "tables": [
            {
                "table": "mart_general_brand_metric",
                "row_count": 2,
                "canonical_sha256": "expected",
                "groups": {"ubist|sales": 2},
            }
        ]
    }

    monkeypatch.setattr(mart_import_ops, "table_exists", lambda *args: True)
    monkeypatch.setattr(
        mart_import_ops,
        "canonical_reference_digest",
        lambda *args: CanonicalDigest(row_count=2, sha256="actual"),
    )
    monkeypatch.setattr(mart_import_ops, "fetch_group_counts", lambda *args: {("ubist", "sales"): 2})

    try:
        mart_import_ops.verify_against_manifest(object(), target_db="target_db", manifest=manifest)
    except RuntimeError as exc:
        assert "canonical checksum mismatch" in str(exc)
    else:
        raise AssertionError("expected digest mismatch to fail")


def test_direct_import_target_absence_guard_rejects_existing_table(monkeypatch) -> None:
    monkeypatch.setattr(mart_import_ops, "table_exists", lambda _conn, _db, table: table == "mart_brand_molecule")

    try:
        mart_import_ops.ensure_direct_import_target_absent(
            object(),
            target_db="jw_mart",
            tables=("mart_general_brand_metric", "mart_brand_molecule"),
        )
    except RuntimeError as exc:
        assert "already exists" in str(exc)
        assert "mart_brand_molecule" in str(exc)
    else:
        raise AssertionError("expected existing target table to fail")


def test_restore_dump_uses_password_env_not_command_args_and_strips_transactions(tmp_path, monkeypatch) -> None:
    dump_path = tmp_path / "mart.sql"
    dump_path.write_text(
        "\n".join(
            [
                "CREATE TABLE mart_general_brand_metric (id int);",
                "SET @OLD_AUTOCOMMIT=@@AUTOCOMMIT, @@AUTOCOMMIT=0;",
                "INSERT INTO mart_general_brand_metric VALUES (1);",
                "COMMIT;",
                "SET AUTOCOMMIT=@OLD_AUTOCOMMIT;",
                "",
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class CapturingStdin(io.BytesIO):
        def close(self) -> None:
            captured["stdin_bytes"] = self.getvalue()
            super().close()

    class FakeProcess:
        def __init__(self, command: list[str], *, stdin: object, env: dict[str, str]) -> None:
            captured["command"] = command
            captured["stdin_arg"] = stdin
            captured["env"] = env
            self.stdin = CapturingStdin()

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            captured["killed"] = True

    monkeypatch.setattr(mart_import_ops.shutil, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None)
    monkeypatch.setattr(
        mart_import_ops,
        "_db_env",
        lambda: {
            "MARIADB_HOST": "db-host",
            "MARIADB_PORT": "3306",
            "MARIADB_USER": "llmops",
            "MARIADB_PASSWORD": "placeholder-value",
        },
    )
    monkeypatch.setattr(mart_import_ops.subprocess, "Popen", FakeProcess)

    result = mart_import_ops.restore_dump_into_schema(target_db="jw_mart", dump_path=dump_path)

    assert result.size_bytes == dump_path.stat().st_size
    assert captured["stdin_bytes"] == (
        b"CREATE TABLE mart_general_brand_metric (id int);\n"
        b"INSERT INTO mart_general_brand_metric VALUES (1);\n"
    )
    assert captured["command"] == [
        "/usr/bin/mariadb",
        "--host=db-host",
        "--port=3306",
        "--user=llmops",
        "jw_mart",
    ]
    assert "placeholder-value" not in " ".join(captured["command"])
    assert captured["env"]["MYSQL_PWD"] == "placeholder-value"


def test_restore_gzip_dump_decompresses_before_client(tmp_path, monkeypatch) -> None:
    dump_path = tmp_path / "mart.sql.gz"
    sql = (
        b"CREATE TABLE mart_general_brand_metric (id int);\n"
        b"SET @OLD_AUTOCOMMIT=@@AUTOCOMMIT, @@AUTOCOMMIT=0;\n"
        b"INSERT INTO mart_general_brand_metric VALUES (1);\n"
        b"COMMIT;\n"
        b"SET AUTOCOMMIT=@OLD_AUTOCOMMIT;\n"
    )
    with gzip.open(dump_path, "wb") as handle:
        handle.write(sql)
    captured: dict[str, object] = {}

    class CapturingStdin(io.BytesIO):
        def close(self) -> None:
            captured["stdin_bytes"] = self.getvalue()
            super().close()

    class FakeProcess:
        def __init__(self, command: list[str], *, stdin: object, env: dict[str, str]) -> None:
            captured["command"] = command
            captured["stdin_arg"] = stdin
            captured["env"] = env
            self.stdin = CapturingStdin()

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            captured["killed"] = True

    monkeypatch.setattr(mart_import_ops.shutil, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None)
    monkeypatch.setattr(
        mart_import_ops,
        "_db_env",
        lambda: {
            "MARIADB_HOST": "db-host",
            "MARIADB_PORT": "3306",
            "MARIADB_USER": "llmops",
            "MARIADB_PASSWORD": "placeholder-value",
        },
    )
    monkeypatch.setattr(mart_import_ops.subprocess, "Popen", FakeProcess)

    result = mart_import_ops.restore_dump_into_schema(target_db="jw_mart", dump_path=dump_path)

    assert result.size_bytes == dump_path.stat().st_size
    assert captured["stdin_bytes"] == (
        b"CREATE TABLE mart_general_brand_metric (id int);\n"
        b"INSERT INTO mart_general_brand_metric VALUES (1);\n"
    )
    assert captured["command"] == [
        "/usr/bin/mariadb",
        "--host=db-host",
        "--port=3306",
        "--user=llmops",
        "jw_mart",
    ]
    assert "placeholder-value" not in " ".join(captured["command"])
    assert captured["env"]["MYSQL_PWD"] == "placeholder-value"


def test_dump_tables_compresses_gzip_and_uses_writeset_safe_options(tmp_path, monkeypatch) -> None:
    dump_path = tmp_path / "mart.sql.gz"
    sql = b"CREATE TABLE mart_general_brand_metric (id int);\n"
    captured: dict[str, object] = {}

    class FakeDumpProcess:
        def __init__(self, command: list[str], *, stdout: object, env: dict[str, str]) -> None:
            captured["command"] = command
            captured["stdout_arg"] = stdout
            captured["env"] = env
            self.stdout = io.BytesIO(sql)

        def __enter__(self) -> "FakeDumpProcess":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(mart_load_ops.shutil, "which", lambda name: "/usr/bin/mariadb-dump" if name == "mariadb-dump" else None)
    monkeypatch.setattr(
        mart_load_ops,
        "_db_env",
        lambda: {
            "MARIADB_HOST": "db-host",
            "MARIADB_PORT": "3306",
            "MARIADB_USER": "llmops",
            "MARIADB_PASSWORD": "placeholder-value",
        },
    )
    monkeypatch.setattr(mart_load_ops.subprocess, "Popen", FakeDumpProcess)

    result = mart_load_ops.dump_tables(
        target_db="build_db",
        tables=("mart_general_brand_metric",),
        dump_path=dump_path,
    )

    assert result.size_bytes == dump_path.stat().st_size
    assert gzip.open(dump_path, "rb").read() == sql
    assert "--skip-add-locks" in captured["command"]
    assert "--skip-extended-insert" in captured["command"]
    assert "--skip-disable-keys" in captured["command"]
    assert "--skip-no-autocommit" in captured["command"]
    assert "placeholder-value" not in " ".join(captured["command"])
    assert captured["env"]["MYSQL_PWD"] == "placeholder-value"
