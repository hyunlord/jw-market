from __future__ import annotations

import pytest

from pipeline.orchestrator.full_rehearsal_provision import (
    FullRehearsalProvisionConfig,
    ProvisionContractError,
    provision_full_rehearsal_databases,
)


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commits = 0
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _config(**overrides: str) -> FullRehearsalProvisionConfig:
    values = {
        "host": "mariadb.internal",
        "port": 3306,
        "root_password": "root-secret",
        "writer_user": "jw_mart_d2_writer",
        "target_db": "jw_mart_rehearsal_r1_fresh",
        "cache_db": "jw_mart_s6_rehearsal_r1_fresh",
    }
    values.update(overrides)
    return FullRehearsalProvisionConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_db", "jw_mart_d2_stage_20260630_r2"),
        ("cache_db", "jw_mart_rehearsal_r1_cache"),
        ("target_db", "jw_mart_rehearsal_r1;DROP DATABASE prod"),
        ("writer_user", "writer'@'localhost"),
    ],
)
def test_provision_rejects_unsafe_or_non_rehearsal_coordinates(
    field: str,
    value: str,
) -> None:
    config = _config(**{field: value})

    with pytest.raises(ProvisionContractError):
        provision_full_rehearsal_databases(config, connect=lambda **_kwargs: None)


def test_provision_rejects_reusing_one_schema_for_mart_and_cache() -> None:
    target = "jw_mart_rehearsal_r1_same"
    config = _config(target_db=target, cache_db=target)

    with pytest.raises(ProvisionContractError):
        provision_full_rehearsal_databases(config, connect=lambda **_kwargs: None)


def test_provision_grants_writer_only_on_the_two_isolated_schemas() -> None:
    connection = _Connection()
    connect_calls: list[dict[str, object]] = []

    def connect(**kwargs: object) -> _Connection:
        connect_calls.append(kwargs)
        return connection

    provision_full_rehearsal_databases(_config(), connect=connect)

    assert connect_calls == [
        {
            "host": "mariadb.internal",
            "port": 3306,
            "user": "root",
            "password": "root-secret",
            "charset": "utf8mb4",
            "autocommit": False,
        }
    ]
    assert connection.cursor_instance.statements == [
        "CREATE DATABASE IF NOT EXISTS `jw_mart_rehearsal_r1_fresh` "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        "GRANT ALL PRIVILEGES ON `jw_mart_rehearsal_r1_fresh`.* "
        "TO 'jw_mart_d2_writer'@'%'",
        "CREATE DATABASE IF NOT EXISTS `jw_mart_s6_rehearsal_r1_fresh` "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        "GRANT ALL PRIVILEGES ON `jw_mart_s6_rehearsal_r1_fresh`.* "
        "TO 'jw_mart_d2_writer'@'%'",
    ]
    assert connection.commits == 1
    assert connection.closed is True
