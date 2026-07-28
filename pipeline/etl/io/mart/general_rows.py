from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from .brand_key_normalize import best_name
from .general_config import UNIT_LABELS
from .general_history import (
    build_audit_code_matrix,
    build_channel_specialty_matrix,
    build_dimensional_history,
    build_products,
    build_sku_dimension_channel_data,
    build_sku_dimension_data,
    cagr_from_history,
    fill_periods,
    hhi_for_period,
    mat_growth,
    pct_growth,
    period_value_map,
    value_at,
)
from .general_window import rolling_period_scope
from .layer3_compute_extended import compute_ei, compute_growth_contribution, compute_hhi, compute_momentum
from .layer3_compute_market_metric import compute_market_mart_payload_from_reduced_rows
from .layer3_normalize import prev_month, prev_quarter_month, safe_div, same_month_prev_year
from .resolve_company import resolve_company


@dataclass(frozen=True)
class UbistAdditivePartial:
    """Lossless pre-derivation state for one product-stable subpartition."""

    frame: pd.DataFrame
    input_rows: int
    atc4_codes: tuple[str, ...]


@dataclass(frozen=True)
class BrandMarketState:
    """ATC-global additive state required before derived metrics are computed."""

    market_periods: dict[str, list[str]]
    market_period_totals: dict[tuple[str, str], float]
    market_history_by_atc: dict[str, dict[str, float]]
    hhi_by_atc_period: dict[tuple[str, str], float | None]
    rank_lookup: dict[tuple[str, str, str], int]


def assert_pre_reduce_minor_units(
    frame: pd.DataFrame,
    columns: Iterable[str],
) -> None:
    """Reject additive state that crossed into floating point before Pass 2."""
    for column in columns:
        if column not in frame.columns:
            raise KeyError(f"missing additive minor-unit column: {column}")
        if not pd.api.types.is_integer_dtype(frame[column].dtype):
            raise TypeError(
                "decimal-additive-v1 pre-reduce float conversion detected: "
                f"{column}={frame[column].dtype}"
            )


def build_brand_period_summary(
    frame: pd.DataFrame,
    *,
    value_column: str,
) -> pd.DataFrame:
    if "brand_key" not in frame.columns:
        raise KeyError("missing required rank tie-break key: brand_key")
    if value_column not in frame.columns:
        raise KeyError(f"missing value column: {value_column}")
    if value_column.endswith("_minor"):
        assert_pre_reduce_minor_units(frame, (value_column,))
    output_column = "raw_value_minor" if value_column.endswith("_minor") else "raw_value"
    working = frame.loc[
        frame[value_column].notna() & (frame[value_column] > 0),
        ["atc4_code", "period_yyyymm", "brand_key", value_column],
    ]
    if working.empty:
        return pd.DataFrame(
            columns=["atc4_code", "period_yyyymm", "brand_key", output_column]
        )
    return (
        working.groupby(
            ["atc4_code", "period_yyyymm", "brand_key"],
            dropna=False,
        )[value_column]
        .sum()
        .rename(output_column)
        .reset_index()
    )


def build_brand_market_state(
    summaries: Iterable[pd.DataFrame],
    *,
    value_column: str = "raw_value",
    minor_unit_scale: Decimal | None = None,
) -> BrandMarketState:
    nonempty = [summary for summary in summaries if not summary.empty]
    if not nonempty:
        return BrandMarketState({}, {}, {}, {}, {})
    rank_source = pd.concat(nonempty, ignore_index=True)
    if "brand_key" not in rank_source.columns:
        raise KeyError("missing required rank tie-break key: brand_key")
    if rank_source["brand_key"].isna().any():
        raise ValueError("rank tie-break key contains null brand_key")
    if value_column.endswith("_minor"):
        assert_pre_reduce_minor_units(rank_source, (value_column,))
        if minor_unit_scale is None or minor_unit_scale <= 0:
            raise ValueError("minor_unit_scale must be positive for minor-unit state")
    elif minor_unit_scale is not None:
        raise ValueError("minor_unit_scale requires a minor-unit value column")
    rank_source = (
        rank_source.groupby(
            ["atc4_code", "period_yyyymm", "brand_key"],
            dropna=False,
        )[value_column]
        .sum()
        .reset_index()
    )
    market_periods = {
        str(atc): fill_periods(part["period_yyyymm"].unique())
        for atc, part in rank_source.groupby("atc4_code", dropna=False)
    }
    market_period_totals = (
        rank_source.groupby(["atc4_code", "period_yyyymm"], dropna=False)[
            value_column
        ]
        .sum()
        .to_dict()
    )
    divisor = float(minor_unit_scale) if minor_unit_scale is not None else 1.0
    market_period_totals = {
        (str(atc), str(period)): float(value) / divisor
        for (atc, period), value in market_period_totals.items()
    }
    market_history_by_atc = {
        str(atc): {
            period: market_period_totals.get((str(atc), period), 0.0)
            for period in market_periods[str(atc)]
        }
        for atc in rank_source["atc4_code"].drop_duplicates()
    }
    hhi_by_atc_period: dict[tuple[str, str], float | None] = {}
    rank_lookup: dict[tuple[str, str, str], int] = {}
    for (atc, period), part in rank_source.groupby(
        ["atc4_code", "period_yyyymm"],
        dropna=False,
    ):
        atc_key = str(atc)
        period_key = str(period)
        exact_total = part[value_column].sum()
        total = float(exact_total)
        ordered_part = part.sort_values("brand_key", kind="mergesort")
        hhi_by_atc_period[(atc_key, period_key)] = (
            compute_hhi(
                [
                    float(value) / total
                    for value in ordered_part[value_column]
                    if total > 0 and float(value) > 0
                ]
            )
            if total > 0
            else None
        )
        ranked = part.sort_values(
            [value_column, "brand_key"],
            ascending=[False, True],
            kind="mergesort",
        ).reset_index(drop=True)
        for index, row in ranked.iterrows():
            rank_lookup[(atc_key, period_key, str(row["brand_key"]))] = index + 1
    return BrandMarketState(
        market_periods=market_periods,
        market_period_totals=market_period_totals,
        market_history_by_atc=market_history_by_atc,
        hhi_by_atc_period=hhi_by_atc_period,
        rank_lookup=rank_lookup,
    )


def build_ubist_additive_partial(frame: pd.DataFrame) -> UbistAdditivePartial:
    assert_pre_reduce_minor_units(
        frame,
        ("raw_sales_minor", "raw_volume_minor"),
    )
    codes = tuple(
        sorted(
            {
                str(value)
                for value in frame.get("atc4_code", pd.Series(dtype=object)).dropna().tolist()
            }
        )
    )
    return UbistAdditivePartial(
        frame=frame,
        input_rows=int(len(frame)),
        atc4_codes=codes,
    )


def reduce_ubist_additive_partials(
    atc4_code: str,
    partials: Iterable[UbistAdditivePartial],
) -> pd.DataFrame:
    """Merge product-stable partials into one ATC4 state before HHI/rank/MS."""
    frames: list[pd.DataFrame] = []
    for partial in partials:
        if partial.atc4_codes and partial.atc4_codes != (atc4_code,):
            raise ValueError(
                f"partial crosses ATC4 boundary: expected={atc4_code} "
                f"actual={partial.atc4_codes}"
            )
        if not partial.frame.empty:
            assert_pre_reduce_minor_units(
                partial.frame,
                ("raw_sales_minor", "raw_volume_minor"),
            )
            frames.append(partial.frame)
    if not frames:
        return pd.DataFrame()
    reduced = pd.concat(frames, ignore_index=True)
    sort_columns = [
        column
        for column in ("product_code", "period_yyyymm", "channel", "specialty")
        if column in reduced.columns
    ]
    if sort_columns:
        reduced = reduced.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)
    return reduced


def _representative_row(group: pd.DataFrame) -> dict[str, Any]:
    """Choose display/company fields deterministically without changing aggregates.

    The archive used first-row selection, so display/company could drift with
    input order. The mart now uses the top sales contributor as the
    representative and breaks ties by the stable audit/product code.
    """
    sort_frame = pd.DataFrame(index=group.index)
    priority_source = (
        group["display_priority_value_minor"]
        if "display_priority_value_minor" in group.columns
        else group["display_priority_value"]
        if "display_priority_value" in group.columns
        else group["raw_sales"]
        if "raw_sales" in group.columns
        else group["raw_value"]
    )
    sort_frame["display_priority_value"] = pd.to_numeric(priority_source, errors="coerce").fillna(0.0)
    for column in ("audit_code", "product_code", "product_name", "brand_name"):
        if column in group.columns:
            sort_frame[column] = group[column].fillna("").astype(str)
        else:
            sort_frame[column] = ""
    order = sort_frame.sort_values(
        ["display_priority_value", "audit_code", "product_code", "product_name", "brand_name"],
        ascending=[False, True, True, True, True],
        kind="mergesort",
    ).index
    return group.loc[order[0]].to_dict()

def build_brand_rows(
    source: str,
    measure: str,
    frame: pd.DataFrame,
    catalog_map: dict[str, dict[str, Any]],
    *,
    value_column: str = "raw_value",
    market_state: BrandMarketState | None = None,
    minor_unit_scale: Decimal | None = None,
) -> list[dict[str, Any]]:
    if value_column not in frame.columns:
        raise KeyError(f"missing value column: {value_column}")
    if value_column.endswith("_minor"):
        assert_pre_reduce_minor_units(frame, (value_column,))
        if minor_unit_scale is None or minor_unit_scale <= 0:
            raise ValueError("minor_unit_scale must be positive for minor-unit rows")
    elif minor_unit_scale is not None:
        raise ValueError("minor_unit_scale requires a minor-unit value column")
    mask = frame[value_column].notna() & (frame[value_column] > 0)
    working = frame.loc[mask].copy(deep=False)
    if minor_unit_scale is not None:
        working = working.assign(
            raw_value_minor=working[value_column],
            raw_value=working[value_column] / float(minor_unit_scale)
        )
    elif value_column != "raw_value":
        working = working.assign(raw_value=working[value_column])
    if working.empty:
        return []
    if market_state is None:
        market_state = build_brand_market_state(
            [build_brand_period_summary(working, value_column=value_column)],
            value_column=(
                "raw_value_minor" if value_column.endswith("_minor") else "raw_value"
            ),
            minor_unit_scale=minor_unit_scale,
        )
    market_periods = market_state.market_periods
    market_period_totals = market_state.market_period_totals
    market_history_by_atc = market_state.market_history_by_atc
    hhi_by_atc_period = market_state.hhi_by_atc_period
    rank_lookup = market_state.rank_lookup
    rows: list[dict[str, Any]] = []
    for (brand_key, atc4_code), group in working.groupby(["brand_key", "atc4_code"], dropna=False):
        atc_key = str(atc4_code)
        periods = market_periods.get(atc_key, fill_periods(group["period_yyyymm"].unique()))
        history = period_value_map(group, periods)
        atc_history = market_history_by_atc.get(atc_key, {})
        metric_history: dict[str, dict[str, Any]] = {}
        extended_history: dict[str, dict[str, Any]] = {}
        ms_values: list[float] = []
        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_period_totals.get((atc_key, period), 0.0)
            ms = safe_div(value, market_total)
            ms_pct = ms * 100 if ms is not None else 0.0
            ms_values.append(ms_pct)
            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_prev_y = value_at(atc_history, same_month_prev_year(period))
            market_growth_abs = atc_history.get(period, 0.0) - market_prev_y if market_prev_y is not None else None
            gc, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(atc_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)
            rank = rank_lookup.get((str(atc4_code), str(period), str(brand_key)))
            metric_history[period] = {
                "raw_value": value,
                "ms": ms_pct,
                "mom": pct_growth(value, prev),
                "qoq": pct_growth(value, prev_q),
                "yoy": pct_growth(value, prev_y),
                "mat": mat_growth(history, period),
                "growth_abs": growth_abs,
                "rank": rank,
            }
            extended_history[period] = {
                "cagr_1y": cagr_from_history(history, period, 1),
                "cagr_3y": cagr_from_history(history, period, 3),
                "cagr_5y": cagr_5y,
                "ei_5y": ei_5y,
                "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                "growth_contribution": gc,
                "growth_contribution_pct": gc,
                "hhi": hhi_by_atc_period.get((str(atc4_code), period)),
                "market_cagr_5y": market_cagr_5y,
                "warnings": [w for w in (gc_warning, ei_warning) if w],
            }
        display_periods = (
            rolling_period_scope(periods, source=source, purpose="display")
            if source == "iqvia_nsa"
            else tuple(periods)
        )
        metric_history = {period: metric_history[period] for period in display_periods}
        extended_history = {
            period: extended_history[period] for period in display_periods
        }
        display_history = {period: history[period] for period in display_periods}
        first = _representative_row(group)
        catalog_row = catalog_map.get(str(brand_key))
        company = resolve_company(catalog_row, first, source)
        by_dimension = {
            "company": company,
            "manufacturer": first.get("manufacturer"),
            "raw_company": first.get("company"),
            "products": build_products(group, list(display_periods)),
            "catalog_status": "matched" if catalog_row else "unmatched",
            "catalog_brand_id": catalog_row.get("brand_id") if catalog_row else None,
            "atc4_code": str(atc4_code),
            "atc4_desc": first.get("atc4_desc"),
        }
        rows.append(
            {
                "brand_key": str(brand_key),
                "brand_name": best_name(first.get("brand_name"), brand_key),
                "atc4_code": str(atc4_code),
                "atc4_desc": first.get("atc4_desc"),
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "metric_history": metric_history,
                "extended_metric_history": extended_history,
                "channel_data": build_dimensional_history(group, "channel", list(display_periods)),
                "specialty_data": build_dimensional_history(group, "specialty", list(display_periods)) if source == "ubist" else {},
                "channel_specialty_matrix": build_channel_specialty_matrix(group, list(display_periods)) if source == "ubist" else {},
                "audit_code_matrix": build_audit_code_matrix(group, list(display_periods)) if source == "iqvia_nsa" else {},
                "dimension_data": build_sku_dimension_data(group, list(display_periods)),
                "dimension_channel_data": build_sku_dimension_channel_data(group, list(display_periods)),
                "by_dimension": by_dimension,
                "raw_value_history": display_history,
                "payload": {
                    "phase": "16-G-4-Fix-Load",
                    "etl_version": "v3.1",
                    "computed_at": datetime.now().isoformat(timespec="seconds"),
                    "row_count": int(len(group)),
                    "period_count": int(len(display_periods)),
                },
            }
        )
    return rows

def build_market_rows(source: str, measure: str, brand_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for atc4_code, grouped in _group_rows(brand_rows, "atc4_code").items():
        atc4_desc = next((row.get("atc4_desc") for row in grouped if row.get("atc4_desc")), None)
        payload = compute_market_mart_payload_from_reduced_rows(
            grouped,
            source=source,
            measure=measure,
            view_type="general",
            catalog_market_row=None,
        )
        rows.append(
            {
                "atc4_code": atc4_code,
                "atc4_desc": atc4_desc,
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "market_size_series": payload["market_size_series"],
                "hhi_series": payload["hhi_series_5y"],
                "brand_ranking": payload["brand_ranking_stacked"],
                "company_ranking_stacked": payload["company_ranking_stacked"],
                "company_concentration_trend": payload["company_concentration_trend"],
                "ei_ms_matrix": payload["ei_ms_matrix"],
                "growth_contribution_ms_matrix": payload["growth_contribution_ms_matrix"],
                "growth_contribution": payload["growth_contribution"],
                "analysis_levels": None,
                "level_top5_trend": None,
                "target_customer_competition": payload["target_customer_competition"],
                "payload": payload["payload"],
            }
        )
    return rows

def _group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key))].append(row)
    return grouped

def restrict_atc4(frame: pd.DataFrame, limit_atc4: int | None) -> pd.DataFrame:
    if not limit_atc4:
        return frame
    values = sorted(v for v in frame["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
    return frame.loc[frame["atc4_code"].isin(values)].copy()

def iter_atc4_chunks(frame: pd.DataFrame, limit_atc4: int | None = None) -> Iterable[tuple[str, pd.DataFrame]]:
    if frame.empty:
        return
    if limit_atc4:
        values = sorted(v for v in frame["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
        frame = frame.loc[frame["atc4_code"].isin(values)]
    for atc4_code, chunk in frame.groupby("atc4_code", dropna=False, sort=True):
        if chunk.empty:
            continue
        chunk_key = "nan" if pd.isna(atc4_code) else str(atc4_code)
        yield chunk_key, chunk.copy()
