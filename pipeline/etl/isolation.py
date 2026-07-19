"""Shared database-name guards for isolated ETL rehearsals."""

from __future__ import annotations


REHEARSAL_DB_PREFIX = "jw_mart_rehearsal_"
PROTECTED_DATABASES = {"jw_mart", "jw_mart_d2_stage_20260630_r2"}


def validate_mart_schema_pair(target_db: str, source_db: str) -> None:
    """Allow self-sourcing only inside the explicit R-1 rehearsal namespace."""

    if target_db in PROTECTED_DATABASES:
        raise ValueError(f"refusing to write mart into protected DB: {target_db}")
    if target_db == source_db and not target_db.startswith(REHEARSAL_DB_PREFIX):
        raise ValueError(f"refusing non-rehearsal self-sourcing mart DB: {target_db}")
