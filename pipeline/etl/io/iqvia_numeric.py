"""Shared IQVIA numeric contracts for source probes and DuckDB consumers."""

from __future__ import annotations

from typing import Final


IQVIA_ENRICH_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("Values LC", "values_lc"),
    ("Units", "units"),
    ("Counting Units", "counting_units"),
    ("Dosage Units", "dosage_units"),
)


def numeric_or_comma_string_to_double_sql(column_expression: str) -> str:
    """Return a DuckDB expression compatible with numeric and comma-string inputs."""

    return (
        "try_cast("
        f"replace(cast({column_expression} AS VARCHAR), ',', '') "
        "AS DOUBLE)"
    )
