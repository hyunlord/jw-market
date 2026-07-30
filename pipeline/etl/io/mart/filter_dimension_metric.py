from __future__ import annotations

"""Build product-level filter dimension sidecars for dynamic general markets.

The existing ``mart_general_brand_metric`` grain intentionally stays at
brand×ATC4×source×measure. Dynamic filters such as UBIST 제형 or 급여구분 are
product-level facts, so applying them to the brand row would over-include every
other product under the same brand. This module emits an additive sidecar with a
product×dimension grain; the dynamic API can aggregate this table only when a
dimension filter is present, while the unfiltered path keeps using the proven
general mart.
"""

from collections.abc import Iterable, Mapping
import math
from typing import TYPE_CHECKING, Any

from pipeline.contracts.dimension_registry import (
    DIMENSION_REGISTRY,
    EMPTY_DIMENSION_VALUES,
    DimensionSpec,
    enabled_dimension_specs,
    normalize_dimension_value,
    normalize_spec_value,
)
from pipeline.contracts.serving_tables import GENERAL_FILTER_DIMENSION_TABLE
from .general_config import MEASURES_BY_SOURCE

if TYPE_CHECKING:
    import pandas as pd


FILTER_DIMENSION_TABLE = GENERAL_FILTER_DIMENSION_TABLE
DIMENSION_STAGE_PREFIX = "jw_mart_dim_stage_"
REHEARSAL_STAGE_PREFIX = "jw_mart_rehearsal_"
LOCAL_SERVING_TARGET = "jw_mart"
BLOCKED_DIMENSION_TARGETS = frozenset(
    {
        "jw_mart",
        "jw_mart_test_stage2",
        "jw_mart_d1_stage_20260625_173115",
    }
)
PERIOD_COMPLETE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "ubist": ("seller", "molecule", "molecule_strength", "form", "route", "reimbursement"),
}


def build_filter_dimension_rows(
    source: str,
    measure: str,
    frame: pd.DataFrame,
    *,
    dimension_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    if measure not in MEASURES_BY_SOURCE.get(source, ()):
        raise ValueError(f"unsupported measure for {source}: {measure}")
    if frame.empty:
        return []
    required = {"atc4_code", "brand_key", "brand_name", "product_code", "period_yyyymm", "raw_value"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"missing columns for filter dimension sidecar: {sorted(missing)}")

    rows: list[dict[str, Any]] = []
    working = frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
    working["source"] = source
    working["measure"] = measure
    specs = enabled_dimension_specs(source)
    if dimension_types is not None:
        enabled_types = {spec.dimension_type for spec in specs}
        unknown = dimension_types.difference(enabled_types)
        if unknown:
            raise ValueError(f"unsupported enabled dimensions for {source}: {sorted(unknown)}")
        specs = tuple(spec for spec in specs if spec.dimension_type in dimension_types)
    for spec in specs:
        label_col = f"__{spec.dimension_type}_display"
        norm_col = f"__{spec.dimension_type}_norm"
        working[label_col] = _dimension_display_series(working, spec)
        working[norm_col] = working[label_col].map(normalize_dimension_value)
        dim_frame = working.loc[working[norm_col].notna()].copy()
        if dim_frame.empty:
            continue
        group_cols = [
            "source",
            "measure",
            "atc4_code",
            "brand_key",
            "product_code",
            norm_col,
        ]
        for key, group in dim_frame.groupby(group_cols, dropna=False, sort=True):
            history = _period_history(group)
            if not history:
                continue
            source_v, measure_v, atc4, brand_key, product_code, norm = key
            brand_name = _first_sorted_label(group["brand_name"])
            display = _first_sorted_label(group[label_col])
            rows.append(
                {
                    "source": str(source_v),
                    "measure": str(measure_v),
                    "atc4_code": str(atc4),
                    "brand_key": str(brand_key),
                    "brand_name": str(brand_name),
                    "product_code": "" if normalize_dimension_value(product_code) is None else str(product_code),
                    "dimension_type": spec.dimension_type,
                    "dimension_value": str(display),
                    "dimension_value_norm": str(norm),
                    "raw_value_history": history,
                }
            )
    return rows


def validate_filter_dimension_period_coverage(
    source: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_period: str,
    expected_total: float | None = None,
) -> dict[str, dict[str, float | int | str]]:
    """Reject a sidecar slice that cannot represent the mart's latest month."""

    required_dimensions = PERIOD_COMPLETE_DIMENSIONS.get(source)
    if required_dimensions is None:
        raise ValueError(f"period-complete dimensions are not defined for source: {source}")

    coverage = {
        dimension_type: {"period_rows": 0, "period_sum": 0.0, "latest_period": ""}
        for dimension_type in required_dimensions
    }
    for row in rows:
        dimension_type = str(row.get("dimension_type") or "")
        if dimension_type not in coverage:
            continue
        history = row.get("raw_value_history")
        if not isinstance(history, Mapping):
            continue
        periods = sorted(str(period) for period, value in history.items() if value is not None)
        if periods:
            coverage[dimension_type]["latest_period"] = max(
                str(coverage[dimension_type]["latest_period"]),
                periods[-1],
            )
        if expected_period not in history:
            continue
        value = history[expected_period]
        if value is None:
            continue
        coverage[dimension_type]["period_rows"] += 1
        coverage[dimension_type]["period_sum"] += float(value)

    missing = [
        dimension_type
        for dimension_type, item in coverage.items()
        if int(item["period_rows"]) == 0
    ]
    if missing:
        raise RuntimeError(
            f"filter dimension sidecar is missing expected period {expected_period}: {missing}"
        )

    period_mismatches = {
        dimension_type: str(item["latest_period"])
        for dimension_type, item in coverage.items()
        if item["latest_period"] != expected_period
    }
    if period_mismatches:
        raise RuntimeError(
            f"filter dimension sidecar latest period mismatch: "
            f"expected={expected_period}, actual={period_mismatches}"
        )

    if expected_total is not None:
        mismatched = {
            dimension_type: float(item["period_sum"])
            for dimension_type, item in coverage.items()
            if not math.isclose(
                float(item["period_sum"]),
                float(expected_total),
                rel_tol=0.0,
                abs_tol=0.01,
            )
        }
        if mismatched:
            raise RuntimeError(
                f"filter dimension sidecar total mismatch for {expected_period}: "
                f"expected={expected_total}, actual={mismatched}"
            )
    return coverage


def validate_filter_dimension_market_coverage(
    source: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_markets: Mapping[str, tuple[str, float]],
) -> dict[str, dict[str, dict[str, float | int | str]]]:
    """Validate every market against the period and total in the general mart."""

    rows_by_market: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        market = str(row.get("atc4_code") or "")
        if market in expected_markets:
            rows_by_market.setdefault(market, []).append(row)

    results: dict[str, dict[str, dict[str, float | int | str]]] = {}
    for market, (period, total) in expected_markets.items():
        try:
            results[market] = validate_filter_dimension_period_coverage(
                source,
                rows_by_market.get(market, ()),
                expected_period=period,
                expected_total=total,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"market {market}: {exc}") from exc
    return results


def guard_dimension_stage_target(target_db: str, *, allow_local_serving_target: bool = False) -> None:
    """Reject unsafe sidecar targets unless the caller explicitly opens local serving.

    Dimension sidecars are normally built in ``jw_mart_dim_stage_*`` schemas.
    D-1 is the first local serving exercise, so ``jw_mart`` is permitted only
    when the tracked CLI has already proven that it is talking to localhost.
    This keeps the old staging safety default intact and prevents accidental
    operating-Galera writes through the generic builder.
    """

    if allow_local_serving_target and target_db == LOCAL_SERVING_TARGET:
        return
    if target_db in BLOCKED_DIMENSION_TARGETS:
        raise ValueError(f"refusing protected target schema: {target_db}")
    if not target_db.startswith((DIMENSION_STAGE_PREFIX, REHEARSAL_STAGE_PREFIX)):
        raise ValueError(
            f"target_db must start with {DIMENSION_STAGE_PREFIX} or "
            f"{REHEARSAL_STAGE_PREFIX}: {target_db}"
        )
    if "`" in target_db or not target_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe target schema name: {target_db}")


def _dimension_display_series(frame: pd.DataFrame, spec: DimensionSpec) -> pd.Series:
    """Return the first non-empty configured source column without row-wise apply.

    Full UBIST+IQVIA builds walk millions of raw product-period rows. A row-wise
    ``DataFrame.apply`` made STAGE B spend minutes before the first Galera-safe
    insert. This vectorized combine keeps the same fallback order while making
    the tracked builder viable for full isolated evidence builds.
    """
    import pandas as pd

    result = pd.Series([None] * len(frame), index=frame.index, dtype=object)
    for column in spec.source_columns:
        if column not in frame:
            continue
        values = frame[column].map(lambda value: normalize_spec_value(value, spec))
        if spec.dimension_type == "atc3":
            values = values.map(_atc3_from_atc4)
        result = result.where(result.notna(), values)
    return result


def _atc3_from_atc4(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().upper()
    return text[:4] if len(text) >= 4 else None


def _first_sorted_label(series: pd.Series) -> str:
    labels = sorted({str(value) for value in series.dropna().tolist() if normalize_dimension_value(value)})
    return labels[0] if labels else ""


def _period_history(group: pd.DataFrame) -> dict[str, float]:
    series = group.groupby("period_yyyymm", dropna=False)["raw_value"].sum().to_dict()
    return {
        str(period): float(value)
        for period, value in sorted(series.items(), key=lambda item: str(item[0]))
        if period and value and float(value) > 0
    }


def summarize_dimension_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"row_count": 0, "dimension_types": {}}
    options: dict[str, set[str]] = {}
    for row in rows:
        summary["row_count"] += 1
        dim_type = str(row["dimension_type"])
        options.setdefault(dim_type, set()).add(str(row["dimension_value_norm"]))
    summary["dimension_types"] = {
        dim_type: {"option_count": len(values)}
        for dim_type, values in sorted(options.items())
    }
    return summary
