"""Fail-closed database credential checks for ingest Jobs."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

PASSWORD_ENV_NAMES = (
    "DB_ROOT_PASSWORD",
    "MARIADB_PASSWORD",
    "AGENT3_DB_PASSWORD",
)


class DBCredentialPreflightError(RuntimeError):
    """A credential contract or connectivity check failed without exposing values."""


def _validate_password_environment(environ: Mapping[str, str]) -> None:
    missing = [name for name in PASSWORD_ENV_NAMES if not environ.get(name)]
    if missing:
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            f"missing_or_empty={','.join(missing)}"
        )

    values = {environ[name] for name in PASSWORD_ENV_NAMES}
    if len(values) != 1:
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            "password_values=mismatch"
        )


def _default_connect() -> Any:
    from pipeline.scripts.ingest_hook import config

    return config.open_mart_connection()


def run_preflight(
    *,
    environ: Mapping[str, str] | None = None,
    connect: Callable[[], Any] | None = None,
) -> None:
    """Validate password aliases and prove the configured DB accepts a query."""
    _validate_password_environment(environ if environ is not None else os.environ)
    connector = connect or _default_connect
    connection = None
    cursor = None
    try:
        connection = connector()
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        if cursor.fetchone() is None:
            raise RuntimeError("SELECT 1 returned no row")
    except DBCredentialPreflightError:
        raise
    except Exception as exc:
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            f"database_probe=failed error_type={type(exc).__name__}"
        ) from None
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
