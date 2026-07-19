"""Provision isolated schemas for pipeline rehearsals."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymysql


TARGET_PREFIX = "jw_mart_rehearsal_"
CACHE_PREFIX = "jw_mart_s6_rehearsal_"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


class ProvisionContractError(ValueError):
    """Raised before connecting when provisioning coordinates are unsafe."""


@dataclass(frozen=True)
class RehearsalProvisionConfig:
    host: str
    port: int
    root_password: str
    writer_user: str
    target_db: str
    cache_db: str


def _validate_identifier(value: str, *, label: str, prefix: str | None = None) -> None:
    if not value or not _IDENTIFIER_RE.fullmatch(value):
        raise ProvisionContractError(f"{label} must be a non-empty SQL identifier")
    if prefix is not None and not value.startswith(prefix):
        raise ProvisionContractError(f"{label} must start with {prefix!r}")


def _validate(config: RehearsalProvisionConfig) -> None:
    if not config.host:
        raise ProvisionContractError("host must not be empty")
    if not 1 <= config.port <= 65535:
        raise ProvisionContractError("port must be between 1 and 65535")
    if not config.root_password:
        raise ProvisionContractError("root password must not be empty")
    _validate_identifier(config.writer_user, label="writer user")
    _validate_identifier(config.target_db, label="target database", prefix=TARGET_PREFIX)
    _validate_identifier(config.cache_db, label="cache database", prefix=CACHE_PREFIX)
    if config.target_db == config.cache_db:
        raise ProvisionContractError("target and cache databases must differ")


def _statements(config: RehearsalProvisionConfig) -> tuple[str, ...]:
    statements: list[str] = []
    for database in (config.target_db, config.cache_db):
        statements.extend(
            (
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
                f"GRANT ALL PRIVILEGES ON `{database}`.* "
                f"TO '{config.writer_user}'@'%'",
            )
        )
    return tuple(statements)


def provision_rehearsal_databases(
    config: RehearsalProvisionConfig,
    *,
    connect: Callable[..., Any] = pymysql.connect,
) -> None:
    """Create and grant only the two prefix-constrained rehearsal schemas."""

    _validate(config)
    connection = connect(
        host=config.host,
        port=config.port,
        user="root",
        password=config.root_password,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            for statement in _statements(config):
                cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()
