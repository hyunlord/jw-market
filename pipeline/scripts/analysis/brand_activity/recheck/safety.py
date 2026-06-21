from __future__ import annotations


STAGE_SCHEMA = "jw_brand_activity_stage"


def require_stage_schema(schema: str) -> str:
    """Enforce the PL-approved isolated schema exactly before any DB write."""
    if schema != STAGE_SCHEMA:
        raise ValueError(f"refusing schema outside {STAGE_SCHEMA}: {schema!r}")
    return schema
