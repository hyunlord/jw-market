from pipeline.scripts.ingest_hook import test_run_census
from pipeline.scripts.ingest_hook.test_run_census import _latest_ranks


def test_latest_ranks_reads_general_mart_period_map():
    assert _latest_ranks(
        {
            "2026-01": [
                {"brand_key": "A", "rank": 2},
                {"brand_key": "B", "rank": 1},
            ],
            "2026-02": [
                {"brand_key": "A", "rank": 1},
                {"brand_key": "B", "rank": 2},
            ],
        }
    ) == {"A": 1, "B": 2}


def test_latest_ranks_accepts_serialized_period_map():
    assert _latest_ranks(
        '{"2026-01":[{"brand_key":"A","rank":1}]}'
    ) == {"A": 1}


def test_census_connections_use_the_explicit_disposable_environment(monkeypatch):
    observed = []

    def connect(**kwargs):
        observed.append(kwargs)
        return object()

    monkeypatch.setattr(test_run_census.pymysql, "connect", connect)
    monkeypatch.setenv("MARIADB_HOST", "forbidden-process-host")

    test_run_census._connect(
        "jw_mart_test_run",
        {
            "MARIADB_HOST": "127.0.0.1",
            "MARIADB_PORT": "3307",
            "MARIADB_USER": "root",
            "MARIADB_PASSWORD": "local-only",
        },
    )

    assert observed == [
        {
            "host": "127.0.0.1",
            "port": 3307,
            "user": "root",
            "password": "local-only",
            "database": "jw_mart_test_run",
            "charset": "utf8mb4",
            "cursorclass": test_run_census.pymysql.cursors.DictCursor,
        }
    ]


def test_census_uses_process_environment_when_explicit_environment_is_absent(
    monkeypatch,
):
    connections = []

    class Connection:
        def close(self):
            return None

    def connect(database, environ):
        connections.append((database, environ))
        return Connection()

    monkeypatch.setenv("MARIADB_SOURCE_DATABASE", "jw_mart_test_source")
    monkeypatch.setenv("INGEST_SHADOW_TARGET_DB", "jw_mart_test_target")
    monkeypatch.setattr(test_run_census, "_connect", connect)
    monkeypatch.setattr(test_run_census, "_market_rows", lambda connection: {})
    monkeypatch.setattr(test_run_census, "_members", lambda connection: {})

    result = test_run_census.build_change_census()

    assert result["changed_markets"] == 0
    assert [database for database, _ in connections] == [
        "jw_mart_test_source",
        "jw_mart_test_target",
    ]
