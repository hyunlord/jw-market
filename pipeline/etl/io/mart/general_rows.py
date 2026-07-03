from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

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
from .layer3_compute_extended import compute_ei, compute_growth_contribution, compute_momentum
from .layer3_compute_market_metric import compute_market_mart_payload
from .layer3_normalize import prev_month, prev_quarter_month, safe_div, same_month_prev_year
from .resolve_company import resolve_company

def _representative_row(group: pd.DataFrame) -> dict[str, Any]:
    """Choose display/company fields deterministically without changing aggregates.

    The archive used first-row selection, so display/company could drift with
    input order. The mart now uses the top sales contributor as the
    representative and breaks ties by the stable audit/product code.
    """
    sort_frame = pd.DataFrame(index=group.index)
    priority_source = (
        group["display_priority_value"]
        if "display_priority_value" in group.columns
        else group["raw_sales"] if "raw_sales" in group.columns else group["raw_value"]
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

def build_brand_rows(source: str, measure: str, frame: pd.DataFrame, catalog_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    working = frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
    if working.empty:
        return []
    market_periods = {
        atc: fill_periods(part["period_yyyymm"].unique())
        for atc, part in working.groupby("atc4_code", dropna=False)
    }
    market_period_totals = working.groupby(["atc4_code", "period_yyyymm"], dropna=False)["raw_value"].sum().to_dict()
    market_history_by_atc = {
        atc: period_value_map(part, market_periods[atc])
        for atc, part in working.groupby("atc4_code", dropna=False)
    }
    hhi_by_atc_period = {
        (str(atc), str(period)): hhi_for_period(part)
        for (atc, period), part in working.groupby(["atc4_code", "period_yyyymm"], dropna=False)
    }
    rank_lookup: dict[tuple[str, str, str], int] = {}
    rank_source = working.groupby(["atc4_code", "period_yyyymm", "brand_key"], dropna=False)["raw_value"].sum().reset_index()
    for (atc, period), part in rank_source.groupby(["atc4_code", "period_yyyymm"], dropna=False):
        part = part.sort_values("raw_value", ascending=False).reset_index(drop=True)
        for idx, row in part.iterrows():
            rank_lookup[(str(atc), str(period), str(row["brand_key"]))] = int(idx + 1)
    rows: list[dict[str, Any]] = []
    for (brand_key, atc4_code), group in working.groupby(["brand_key", "atc4_code"], dropna=False):
        periods = market_periods.get(atc4_code, fill_periods(group["period_yyyymm"].unique()))
        history = period_value_map(group, periods)
        atc_history = market_history_by_atc.get(atc4_code, {})
        metric_history: dict[str, dict[str, Any]] = {}
        extended_history: dict[str, dict[str, Any]] = {}
        ms_values: list[float] = []
        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_period_totals.get((atc4_code, period), 0.0)
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
        first = _representative_row(group)
        catalog_row = catalog_map.get(str(brand_key))
        company = resolve_company(catalog_row, first, source)
        by_dimension = {
            "company": company,
            "manufacturer": first.get("manufacturer"),
            "raw_company": first.get("company"),
            "products": build_products(group, periods),
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
                "channel_data": build_dimensional_history(group, "channel", periods),
                "specialty_data": build_dimensional_history(group, "specialty", periods) if source == "ubist" else {},
                "channel_specialty_matrix": build_channel_specialty_matrix(group, periods) if source == "ubist" else {},
                "audit_code_matrix": build_audit_code_matrix(group, periods) if source == "iqvia_nsa" else {},
                "dimension_data": build_sku_dimension_data(group, periods),
                "dimension_channel_data": build_sku_dimension_channel_data(group, periods),
                "by_dimension": by_dimension,
                "raw_value_history": history,
                "payload": {
                    "phase": "16-G-4-Fix-Load",
                    "etl_version": "v3.1",
                    "computed_at": datetime.now().isoformat(timespec="seconds"),
                    "row_count": int(len(group)),
                    "period_count": int(len(periods)),
                },
            }
        )
    return rows

def build_market_rows(source: str, measure: str, brand_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for atc4_code, grouped in _group_rows(brand_rows, "atc4_code").items():
        atc4_desc = next((row.get("atc4_desc") for row in grouped if row.get("atc4_desc")), None)
        payload = compute_market_mart_payload(grouped, source=source, measure=measure, view_type="general", catalog_market_row=None)
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
