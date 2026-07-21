"""Shared database-name guards for isolated ETL rehearsals."""

from __future__ import annotations

from pipeline.scripts.utils.mart_config import PROTECTED_MART_DB_NAMES


REHEARSAL_DB_PREFIX = "jw_mart_rehearsal_"


def validate_mart_schema_pair(target_db: str, source_db: str) -> None:
    """Allow self-sourcing only inside the explicit R-1 rehearsal namespace."""

    if target_db in PROTECTED_MART_DB_NAMES:
        raise ValueError(f"refusing to write mart into protected DB: {target_db}")
    if target_db == source_db and not target_db.startswith(REHEARSAL_DB_PREFIX):
        raise ValueError(f"refusing non-rehearsal self-sourcing mart DB: {target_db}")
