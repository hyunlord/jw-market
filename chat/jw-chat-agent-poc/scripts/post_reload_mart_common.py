"""Shared helpers for read-only post-reload mart acceptance gates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

try:
    from .post_reload_fdm_values import series
except ImportError:
    from post_reload_fdm_values import series


def census_gate(
    name: str,
    *,
    checked: int,
    population: int,
    failures: Sequence[str],
    tolerance: str,
) -> dict[str, Any]:
    failure_list = list(failures)
    exit_code = int(population == 0 or checked != population or bool(failure_list))
    return {
        "gate": name,
        "classification": "census",
        "checked": checked,
        "population": population,
        "missing": "fail",
        "tolerance": tolerance,
        "failures": failure_list,
        "failure_reasons": failure_list,
        "failure_count": len(failure_list),
        "exit_code": exit_code,
        "environment": "runtime_mart_read_only",
    }


def normalize_utc_iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace(" ", "T")
        if not text:
            return ""
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def marker_for_sql(value: str) -> datetime:
    normalized = normalize_utc_iso(value)
    if not normalized:
        raise ValueError(f"invalid FDM computed_at marker: {value!r}")
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)


def aggregate_history_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    aggregates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        market = str(row.get("market_id") or "").strip()
        dimension = str(row.get("dimension_type") or "").strip()
        key = (market, dimension)
        aggregate = aggregates.setdefault(
            key,
            {
                "market_id": market,
                "dimension_type": dimension,
                "raw_value_history": {},
                "source_row_count": 0,
            },
        )
        aggregate["source_row_count"] += 1
        history = aggregate["raw_value_history"]
        for period, value in series(row.get("raw_value_history")).items():
            if period not in history:
                history[period] = value
            elif history[period] is None or value is None:
                history[period] = None
            else:
                history[period] += value
    return [aggregates[key] for key in sorted(aggregates)]
