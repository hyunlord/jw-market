"""Fail-closed database credential checks for ingest Jobs."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

PASSWORD_ENV_NAMES = (
    "DB_ROOT_PASSWORD",
    "DB_PASSWORD",
    "MARIADB_PASSWORD",
    "AGENT3_DB_PASSWORD",
)


class DBCredentialPreflightError(RuntimeError):
    """A credential contract or connectivity check failed without exposing values."""


def _validate_password_environment(environ: Mapping[str, str]) -> None:
    missing = [name for name in PASSWORD_ENV_NAMES if not environ.get(name)]
    if missing:
        connector = (
            "shortlong_dynamic_market_api"
            if "DB_PASSWORD" in missing
            else "password_alias_contract"
        )
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            f"connector={connector} "
            f"missing_or_empty={','.join(missing)}"
        )

    values = {environ[name] for name in PASSWORD_ENV_NAMES}
    if len(values) != 1:
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            "connector=password_alias_contract password_values=mismatch"
        )


def _default_connect() -> Any:
    from pipeline.scripts.ingest_hook import config

    return config.open_mart_connection()


def _default_shortlong_connectors() -> Mapping[str, Callable[[], Any]]:
    from pipeline.scripts.api import db as api_db
    from pipeline.scripts.ai_analysis.agent2_regen_orchestrator import (
        PHASE_ZETA_ROOT,
        BundleConfig,
        RunnerConfig,
        _connect_bundle_db,
        _connect_runner_db,
    )

    bundle_config = BundleConfig.from_yaml(
        PHASE_ZETA_ROOT / "configs" / "phase_zeta_v1_1.yaml"
    )
    runner_config = RunnerConfig.from_yaml(
        PHASE_ZETA_ROOT / "configs" / "genos_runner_v1.yaml"
    )
    return {
        "shortlong_bundle": lambda: _connect_bundle_db(bundle_config),
        "shortlong_runner": lambda: _connect_runner_db(runner_config),
        "shortlong_dynamic_market_api": api_db.connect,
    }


def _probe_connector(name: str, connector: Callable[[], Any]) -> None:
    connection = None
    cursor = None
    try:
        connection = connector()
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        if cursor.fetchone() is None:
            raise RuntimeError("SELECT 1 returned no row")
    except Exception as exc:
        raise DBCredentialPreflightError(
            "DB credential preflight blocked before g3: "
            f"connector={name} database_probe=failed "
            f"error_type={type(exc).__name__}"
        ) from None
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def run_preflight(
    *,
    environ: Mapping[str, str] | None = None,
    connect: Callable[[], Any] | None = None,
    additional_connectors: Mapping[str, Callable[[], Any]] | None = None,
) -> None:
    """Validate aliases and prove every ingest-reachable DB accepts a query."""
    _validate_password_environment(environ if environ is not None else os.environ)
    connectors: dict[str, Callable[[], Any]] = {
        "mart": connect or _default_connect,
    }
    if additional_connectors is not None:
        connectors.update(additional_connectors)
    elif connect is None:
        connectors.update(_default_shortlong_connectors())

    for name, connector in connectors.items():
        _probe_connector(name, connector)
