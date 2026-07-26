"""Category-scoped serving refresh after an atomic ingest activation."""
from __future__ import annotations

import os
import subprocess
import sys

from pipeline.scripts.ingest_hook.category_map import CategorySpec


PY = sys.executable or "python3"
NORMAL_CACHE_CATEGORIES = frozenset({"ubist", "iqvia_nsa", "mi_master"})


class DownstreamRefreshError(RuntimeError):
    """Raised when a serving refresh command fails."""


def commands(spec: CategorySpec) -> tuple[tuple[str, ...], ...]:
    planned: list[tuple[str, ...]] = []
    if spec.refresh_argv:
        planned.append(spec.refresh_argv)
    if spec.key in NORMAL_CACHE_CATEGORIES:
        planned.extend(
            (
                (PY, "-m", "pipeline.scripts.etl.build_cache_brands"),
                (PY, "-m", "pipeline.scripts.etl.build_cache_market_status"),
            )
        )
    if spec.key == "iqvia_csd_keyword":
        planned.append(
            (
                PY,
                "-m",
                "pipeline.scripts.etl.brand_activity.brand_activity_replay",
                "--only",
                "topic",
                "--execute",
                "--save-to-db",
            )
        )
    return tuple(planned)


def run(spec: CategorySpec) -> None:
    """Execute the declared refresh plan in order and stop on the first failure."""
    for argv in commands(spec):
        _run(argv)


def _run(argv: tuple[str, ...]) -> None:
    env = os.environ.copy()
    production_db = env.get("INGEST_LOAD_PRODUCTION_DB", "").strip()
    if production_db:
        env["MARIADB_DATABASE"] = production_db
        env["DB_NAME"] = production_db
    result = subprocess.run(argv, check=False, env=env)
    if result.returncode != 0:
        raise DownstreamRefreshError(
            f"downstream refresh failed rc={result.returncode}: {' '.join(argv)}"
        )
